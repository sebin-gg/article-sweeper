#!/usr/bin/env python3
"""Fetch article bodies concurrently, *while* classification is still running.

Customer feedback #4: fetching after classification serialized the sweep and
wasted the window where 20 idle connections could already be saturating. This
script takes the typed decisions from `classify_tabs_typed`, fans the fetches
out immediately (default 20 at a time, the `xargs -P 20` shape), and records a
per-URL outcome so the caller can route blocked hosts to search without
re-inspecting anything.

Feedback #5 is honoured here too: paywalled domains are not fetched at all
unless `--allow-search-fallback` is passed, because the fallback is usually
worse than an honest title+domain line.

Reads newline-delimited JSON on stdin (one `{"url":...,"title":...}` per line,
or bare URLs), writes one JSON object per line on stdout:

    {"url":..., "status":"ok"|"blocked"|"error"|"skipped-paywalled",
     "http":200, "bytes":12345, "content_type":"text/html; charset=utf-8",
     "needs_search":false, "reason":"..."}

Exit code is 0 even when individual fetches fail — a blocked page is a normal
outcome the caller routes, not a script failure.

Usage:
    python3 fetch_articles.py --concurrency 20 < urls.jsonl > fetched.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from sweep_lib import (  # noqa: E402
    FETCH_THEN_SEARCH,
    SKIP_FETCH_TITLE_ONLY,
    MIN_WORDS,
    content_signals,
    extract_readable,
    html_to_text,
    looks_like_challenge,
    page_title,
    is_blocked_status,
    plan_fetch,
)

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAX_BYTES = 4 * 1024 * 1024
# Cap the text we keep per page; it is for classification, not archival.
# Cap on the text handed to the summarizer. Most summaries need only the top
# of the piece; uncapped bodies are the largest avoidable prompt cost. Signals
# are still computed from the FULL extracted text so density is not skewed by
# the cap.
MAX_TEXT_CHARS = 16_000

# Per-domain ceiling (customer feedback #4). A single global pool of 20 sends 20
# concurrent hits at any one host, which is exactly how you collect 429s and
# temporary bans — five Medium tabs in a sweep is five simultaneous requests to
# Medium. Concurrency is keyed by registrable host, so breadth still scales
# across many domains while any single one stays polite.
DEFAULT_PER_DOMAIN = 2


def host_of(url: str) -> str:
    """Registrable-ish host for per-domain limiting. '' when unparseable."""
    try:
        h = (urllib.parse.urlparse(url or "").hostname or "").lower().rstrip(".")
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def _bucket_plan(urls, per_domain: int) -> dict:
    """Round-robin the URLs so each host's work starts early and evenly."""
    buckets: dict[str, list] = {}
    for u in urls:
        buckets.setdefault(host_of(u) or u, []).append(u)
    return buckets


class _DomainGates:
    """One semaphore per host, so each host is limited independently."""

    def __init__(self, per_domain: int):
        self._sem = threading.Semaphore(per_domain)
        self._lock = threading.Lock()
        self._gates: dict[str, threading.Semaphore] = {}

    def gate(self, host: str) -> threading.Semaphore:
        with self._lock:
            g = self._gates.get(host)
            if g is None:
                g = threading.Semaphore(self._sem._value)
                self._gates[host] = g
            return g

    def run(self, url: str, fn):
        host = host_of(url) or url
        with self.gate(host):
            return fn(url)


# Statuses worth retrying: transient server-side trouble or explicit throttle.
# 401/402/403/451 are refusals, not transient -- retrying those is pointless and
# looks abusive to the publisher, so they fail immediately (see BLOCKED_STATUS).
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE = 0.75
DEFAULT_BACKOFF_CAP = 8.0


def is_retryable_status(status) -> bool:
    """True for statuses where the same request may succeed shortly."""
    try:
        return int(status) in RETRY_STATUS
    except (TypeError, ValueError):
        return False


def backoff_delay(attempt: int, *, retry_after: str = "",
                  base: float = DEFAULT_BACKOFF_BASE,
                  cap: float = DEFAULT_BACKOFF_CAP) -> float:
    """Exponential backoff with jitter; honours `Retry-After` when sent.

    `attempt` is 1-based. Jitter keeps N tabs against one host from retrying in
    lockstep and re-creating the stampede that caused the 429.
    """
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), cap * 4))
        except (TypeError, ValueError):
            pass
    raw = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(raw / 2.0, raw)


def _retry_after_seconds(headers) -> str:
    try:
        return str(headers.get("Retry-After", "") or "")
    except AttributeError:
        return ""


def fetch_one(url: str, *, timeout: float, allow_search_fallback: bool,
              want_text: bool = False,
              max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict:
    """Fetch one URL with bounded retry. Never raises.

    Every failure becomes a typed outcome. Transient trouble (429/5xx/network) is
    retried with exponential backoff and jitter, honouring `Retry-After`;
    refusals (401/402/403/451) fail immediately, because retrying them only
    annoys the publisher.
    """
    plan = plan_fetch(url, allow_search_fallback=allow_search_fallback)
    if plan == SKIP_FETCH_TITLE_ONLY:
        return {"url": url, "status": "skipped-paywalled", "http": None,
                "needs_search": False, "attempts": 0,
                "reason": "paywalled; fetch skipped by policy"}

    attempts = max(1, int(max_attempts))
    last: dict = {}

    for attempt in range(1, attempts + 1):
        # Request() itself raises ValueError on an unparseable URL, so construction
        # has to sit inside the try -- otherwise one junk input line aborts the run
        # and every other result is lost.
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(MAX_BYTES)
                out = {
                    "url": url,
                    "status": "ok",
                    "http": int(getattr(resp, "status", 200) or 200),
                    "bytes": len(body),
                    "content_type": resp.headers.get("Content-Type", ""),
                    "needs_search": False,
                    "attempts": attempt,
                    "reason": "",
                }
                if want_text:
                    # Same request, no extra cost: the body doubles as evidence
                    # for the still-open `unsure` decision and as summary input.
                    try:
                        raw = body.decode("utf-8", "replace")
                        # Readability pass first; searching is the last resort,
                        # so a JS-heavy page must be exhausted before that.
                        readable = extract_readable(raw)
                        full = readable or html_to_text(raw)
                        # Signals come from the FULL text: capping first would
                        # understate word counts and quietly skew density.
                        out["signals"] = content_signals(full)
                        out["truncated"] = len(full) > MAX_TEXT_CHARS
                        out["text"] = full[:MAX_TEXT_CHARS]
                        # The page's own title beats the CDP-reported one.
                        out["page_title"] = page_title(raw)
                        out["html_chars"] = len(raw)
                        challenged, why = looks_like_challenge(raw, full)
                        out["challenge"] = why if challenged else ""
                        # A JS shell yields almost no prose: say so plainly
                        # instead of handing the summariser a near-empty body.
                        out["js_shell"] = bool(
                            out["signals"].get("words", 0) < MIN_WORDS)
                    except Exception:  # noqa: BLE001 - text is best-effort
                        out["text"] = ""
                        out["signals"] = {}
                        out["truncated"] = False
                        out["page_title"] = ""
                        out["challenge"] = ""
                        out["js_shell"] = False
                return out

        except urllib.error.HTTPError as exc:
            code = int(exc.code)
            blocked = is_blocked_status(code)
            more = is_retryable_status(code) and attempt < attempts
            last = {
                "url": url,
                "status": ("blocked" if blocked
                           else "throttled" if (more or code == 429) else "error"),
                "http": code,
                "bytes": 0,
                "content_type": "",
                "attempts": attempt,
                # Only route to search when the plan promised it; otherwise a
                # blocked page just becomes a short honest title+domain entry.
                "needs_search": blocked and plan == FETCH_THEN_SEARCH,
                "reason": f"HTTP {code}",
            }
            if not more:
                return last
            time.sleep(backoff_delay(attempt,
                                      retry_after=_retry_after_seconds(exc.headers)))

        except Exception as exc:  # noqa: BLE001 - any failure is a typed outcome
            more = attempt < attempts
            last = {
                "url": url,
                "status": "throttled" if more else "error",
                "http": None, "bytes": 0, "content_type": "",
                "attempts": attempt, "needs_search": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }
            if not more:
                return last
            time.sleep(backoff_delay(attempt))

    return last


def _read_inputs(stream):
    for line in stream:
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = obj.get("url")
            if isinstance(url, str) and url:
                yield url
        else:
            yield line


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--concurrency", type=int, default=20,
                    help="total parallel fetches (default 20)")
    ap.add_argument("--per-domain", type=int, default=DEFAULT_PER_DOMAIN,
                    help=f"max parallel fetches to any ONE host (default "
                         f"{DEFAULT_PER_DOMAIN}; global concurrency alone gets "
                         f"you rate-limited and banned)")
    ap.add_argument("--timeout", type=float, default=20.0, help="per-request seconds")
    ap.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
                    help="total tries per URL; transient 429/5xx/network "
                         "are retried with exponential backoff + jitter")
    ap.add_argument("--with-text", action="store_true",
                    help="also emit extracted text + density signals per URL, so "
                         "the same request can resolve `unsure` decisions")
    ap.add_argument("--allow-search-fallback", action="store_true",
                    help="paywalled domains: fetch, then route a 403 to search. "
                         "Off by default (feedback #5).")
    args = ap.parse_args(argv)

    if args.concurrency < 1:
        ap.error("--concurrency must be >= 1")
    if args.per_domain < 1:
        ap.error("--per-domain must be >= 1")
    if args.max_attempts < 1:
        ap.error("--max-attempts must be >= 1")

    urls = list(_read_inputs(sys.stdin))
    if not urls:
        return 0

    gates = _DomainGates(args.per_domain)

    def work(u):
        return gates.run(u, lambda url: fetch_one(
            url, timeout=args.timeout, want_text=args.with_text,
            allow_search_fallback=args.allow_search_fallback,
            max_attempts=args.max_attempts))

    # Breadth still comes from the global pool: many hosts proceed at once, while
    # each host is capped independently. Fetches overlap with whatever the agent
    # is still doing to finish classification.
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(work, u) for u in urls]
        for fut in futures:
            sys.stdout.write(json.dumps(fut.result(), ensure_ascii=False) + "\n")
            sys.stdout.flush()  # stream as results land, not at the end
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
