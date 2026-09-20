"""Deterministic core for article-sweeper.

Covers the guarantees that must NOT depend on agent prose:
URL unwrap + canonicalize, dedupe, article classification baseline,
CDP parsing/validation, close-candidate revalidation, atomic append,
port/host validation, per-browser endpoint registry, log redaction.

All functions are pure/std-lib-only so CI can test them with mocks.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Ports / hosts
# ---------------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"

#: Default debug port per Chromium-family browser. A single global 9222
#: cannot serve several browsers at once, so each browser gets its own.
DEFAULT_PORTS = {
    "thorium": 9222,
    "chromium": 9223,
    "chrome": 9224,
    "brave": 9225,
    "edge": 9226,
    "vivaldi": 9227,
    "opera": 9228,
}


def validate_port(port) -> int:
    """Validate a TCP port. Raises ValueError unless 1 <= port <= 65535."""
    try:
        n = int(str(port).strip())
    except (TypeError, ValueError):
        raise ValueError(f"bad port: {port!r}")
    if not 1 <= n <= 65535:
        raise ValueError(f"bad port (want 1-65535): {port!r}")
    return n


def validate_host(host: str) -> str:
    """Only loopback hosts are supported by design (least privilege)."""
    h = (host or "").strip()
    if h not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"refusing non-loopback CDP host {h!r}: "
            "remote debugging exposes the logged-in session; "
            "only 127.0.0.1/localhost/::1 are supported"
        )
    return "127.0.0.1" if h == "localhost" else h


def find_free_port(exclude: set[int] | None = None) -> int:
    """Return a free loopback TCP port (per-browser endpoint discovery)."""
    exclude = exclude or set()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    if port in exclude:
        return find_free_port(exclude)
    return port


def endpoint_for(browser: str, taken: set[int] | None = None) -> tuple[str, int]:
    """Return (host, port) for a browser, avoiding collisions in `taken`."""
    taken = set(taken or set())
    preferred = DEFAULT_PORTS.get(browser.lower(), 0)
    if preferred and preferred not in taken:
        return DEFAULT_HOST, preferred
    port = find_free_port(taken)
    taken.add(port)
    return DEFAULT_HOST, port


# ---------------------------------------------------------------------------
# URL unwrap / canonicalize
# ---------------------------------------------------------------------------

#: Query params that are pure tracking and safe to drop. Everything else is
#: preserved: pagination, language, revision, content-id params are meaningful.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "gclid", "gbraid", "wbraid", "fbclid", "msclkid", "mc_cid",
    "mc_eid", "igshid", "spm", "_hsenc", "_hsmi", "ref", "referrer",
    "srsltid", "vero_conv", "vero_id", "yclid", "ttclid",
})

#: Query params that must never appear in logs/scratch (tokens, invites).
SENSITIVE_PARAMS = frozenset({
    "token", "auth", "auth_token", "access_token", "id_token", "session",
    "sessionid", "session_id", "key", "api_key", "apikey", "secret",
    "password", "passwd", "invite", "invite_code", "code", "state",
    "sig", "signature", "token_type", "bearer",
})

WRAPPER_DOMAINS = (
    "google.com", "www.google.com",
    "tracking.tldrnewsletter.com", "tracking.inflection.io",
)


def unwrap_tracking_wrapper(url: str) -> str:
    """Unwrap known redirect/tracking wrappers deterministically."""
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return url
    host = (p.hostname or "").lower()
    qs = urllib.parse.parse_qs(p.query, keep_blank_values=True)

    # google.com/url?q=<real>
    if host in ("google.com", "www.google.com") and p.path == "/url":
        real = (qs.get("q") or qs.get("url") or [None])[0]
        if real and re.match(r"https?://", real):
            return real
    # tldr / inflection click-tracking: first absolute-URL param wins
    if host in ("tracking.tldrnewsletter.com", "tracking.inflection.io"):
        for vals in qs.values():
            for v in vals:
                if re.match(r"https?://", v or ""):
                    return v
        # path-embedded target e.g. /CL0/https://example.com/...
        m = re.search(r"/(https?://.+)$", p.path)
        if m:
            return urllib.parse.unquote(m.group(1))
    # kit-mail `lmu...` redirect paths carry the target in `u`/`url`/`redirect`
    for key in ("u", "url", "redirect", "dest", "destination", "to"):
        vals = qs.get(key) or []
        for v in vals:
            if re.match(r"https?://", v or ""):
                return v
    return url


def canonicalize_url(url: str) -> str:
    """Canonical form used for dedupe.

    - unwraps tracking wrappers first
    - lowercases scheme+host, drops default ports
    - drops fragment
    - drops ONLY known tracking params; keeps everything else (pagination,
      lang, revision, content ids) so distinct articles never merge
    - sorts remaining query pairs for stability
    """
    url = unwrap_tracking_wrapper(url.strip())
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return url
    scheme = (p.scheme or "https").lower()
    host = (p.hostname or "").lower()
    if not host:
        return url
    port = p.port
    if port and not ((scheme == "http" and port == 80)
                     or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = p.path or "/"
    # keep meaningful query, drop tracking-only params
    pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    kept = sorted(
        (k, v) for k, v in pairs
        if k not in TRACKING_PARAMS and not k.startswith("utm_")
    )
    query = urllib.parse.urlencode(kept, doseq=True)
    out = urllib.parse.urlunparse((scheme, host, path, "", query, ""))
    return out


def dedupe_key(url: str) -> str:
    """Dedupe identity = canonical URL (query-significant except tracking)."""
    return canonicalize_url(url)


def dedupe_tabs(tabs: list[dict]) -> dict[str, list[dict]]:
    """Group tab records by canonical URL. Returns {canonical: [tabs]}."""
    groups: dict[str, list[dict]] = {}
    for t in tabs:
        key = dedupe_key(str(t.get("url", "")))
        groups.setdefault(key, []).append(t)
    return groups


# ---------------------------------------------------------------------------
# Log redaction
# ---------------------------------------------------------------------------

def redact_url(url: str) -> str:
    """Redact sensitive query values for logs/scratch output."""
    try:
        p = urllib.parse.urlparse(url)
        pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        red = [
            (k, "***" if k.lower() in SENSITIVE_PARAMS else v)
            for k, v in pairs
        ]
        return urllib.parse.urlunparse(
            (p.scheme, p.netloc, p.path, p.params,
             urllib.parse.urlencode(red, doseq=True), p.fragment))
    except Exception:
        return "<unparseable-url>"


def redact_record(rec: dict) -> dict:
    out = dict(rec)
    if "url" in out and isinstance(out["url"], str):
        out["url"] = redact_url(out["url"])
    return out


# ---------------------------------------------------------------------------
# Article classification (deterministic baseline; agent makes final call)
# ---------------------------------------------------------------------------

# Host blocklist: matched against the hostname with dot boundaries (exact
# or subdomain-suffix), never by raw substring — otherwise "ex.com" would
# falsely match an "x.com" rule.
BLOCKED_HOST_EXACT_OR_SUFFIX = frozenset({
    "github.com", "gitlab.com", "bitbucket.org",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "linkedin.com", "reddit.com",
    "outlook.com", "outlook.office.com", "teams.microsoft.com",
    "slack.com", "discord.com", "zoom.us",
    "paypal.com",
})
BLOCKED_HOST_PREFIXES = (
    "mail.google.", "outlook.", "teams.", "slack.", "discord.",
    "meet.google", "calendar.google", "drive.google", "sheets.",
    "slides.", "accounts.google", "login.", "auth.", "sso.",
    "grafana.", "kibana.", "jira.", "linear.", "asana.", "trello.",
    "notion.",
)
BLOCKED_HOST_SUBSTRINGS = ("pornhub", "xnxx", "xvideos", "bank")


def _host_blocked(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    if not h:
        return False
    if h in ("localhost",):
        return True
    try:
        import ipaddress
        ipaddress.ip_address(h)
        return True  # raw IPs (incl. 127.0.0.1) are app/local, not articles
    except ValueError:
        pass
    for b in BLOCKED_HOST_EXACT_OR_SUFFIX:
        if h == b or h.endswith("." + b):
            return True
    for p in BLOCKED_HOST_PREFIXES:
        if h.startswith(p):
            return True
    return any(s in h for s in BLOCKED_HOST_SUBSTRINGS)

NON_ARTICLE_PATH_RE = re.compile(
    r"(/inbox|/mail|/chat|/chats|/messages|/feed$|/feeds/|/dashboard|"
    r"/settings|/account|/billing|/admin|/repos?/|/pulls?/|/issues?/|"
    r"/pipelines|/actions|/deploy|/checkout|/cart|/login|/signin|/signup|"
    r"/oauth|/sso|/status$|/healthz)",
    re.I,
)

INTERNAL_SCHEMES = (
    "devtools://", "chrome://", "chrome-extension://", "edge://",
    "brave://", "opera://", "vivaldi://", "about:", "view-source:",
)

ARTICLE_HINT_RE = re.compile(
    r"(/blog/|/blogs/|/news/|/articles?/|/posts?/|/stories?/|/docs?/|"
    r"/tutorials?/|/guides?/|/papers?/|/releases?/|/changelog|/wiki/|"
    r"/20\d\d/\d\d/|/p/|/story/|/essays?/)",
    re.I,
)


def classify_url(url: str, title: str = "") -> tuple[bool, str]:
    """Baseline classifier: (is_article_candidate, reason).

    Heuristic only — the agent still applies SKILL.md judgment, but this
    gives deterministic, tested behavior instead of prose-only rules.
    """
    u = (url or "").strip()
    low = u.lower()
    for scheme in INTERNAL_SCHEMES:
        if low.startswith(scheme):
            return False, f"internal-scheme:{scheme}"
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return False, "unparseable-url"
    if p.scheme not in ("http", "https"):
        return False, f"non-http-scheme:{p.scheme}"
    host = (p.hostname or "").lower()
    if _host_blocked(host):
        return False, "non-article-host"
    if NON_ARTICLE_PATH_RE.search(p.path):
        return False, "non-article-path"
    if ARTICLE_HINT_RE.search(p.path):
        return True, "article-path-hint"
    # default: candidate article; agent confirms (title/content check)
    if title and len(title.strip()) > 0 and "." in host:
        return True, "default-candidate"
    return False, "weak-signal"


# ---------------------------------------------------------------------------
# CDP parsing / validation
# ---------------------------------------------------------------------------

@dataclass
class TabRecord:
    id: str
    url: str
    title: str = ""
    type: str = "page"
    endpoint: str = ""          # e.g. "127.0.0.1:9224"
    browser: str = ""           # e.g. "chrome"
    canonical: str = field(default="")

    def __post_init__(self):
        self.canonical = dedupe_key(self.url)


def is_internal_target(url: str, ctype: str = "page") -> bool:
    low = (url or "").lower()
    if any(low.startswith(s) for s in INTERNAL_SCHEMES):
        return True
    # Non-page CDP target types (background_page, service_worker, etc.)
    return ctype != "page"


def parse_cdp_list(data, *, endpoint: str = "", browser: str = "") -> list[TabRecord]:
    """Validate + filter a CDP /json/list payload.

    - requires list of objects with string id + url
    - keeps only type == "page" (robust internal-target filter, not just
      a devtools:// substring check)
    - malformed entries raise ValueError (never silently accepted)
    - every record carries endpoint+browser identity so a bare tab id is
      never ambiguous across simultaneous browsers
    """
    if not isinstance(data, list):
        raise ValueError("CDP /json/list must be a JSON array")
    out: list[TabRecord] = []
    for i, d in enumerate(data):
        if not isinstance(d, dict):
            raise ValueError(f"CDP entry {i}: not an object")
        ctype = d.get("type", "")
        url = d.get("url", "")
        tab_id = d.get("id", "")
        if not isinstance(tab_id, str) or not tab_id:
            raise ValueError(f"CDP entry {i}: missing/invalid id")
        if not isinstance(url, str) or not url:
            raise ValueError(f"CDP entry {i}: missing/invalid url")
        if is_internal_target(url, ctype):
            continue
        out.append(TabRecord(
            id=tab_id, url=url, title=str(d.get("title", "") or ""),
            type=str(ctype), endpoint=endpoint, browser=browser,
        ))
    return out


def verify_close_candidates(candidates: list[TabRecord],
                             current: list[TabRecord]) -> tuple[list[TabRecord], list[str]]:
    """Revalidate close set immediately before closing.

    A tab may navigate/close/reopen between enumeration and close, so each
    candidate must still exist in `current` with the SAME canonical URL.
    Returns (safe_to_close, problems).
    """
    by_id = {t.id: t for t in current if t.endpoint == candidates[0].endpoint} \
        if candidates else {}
    # fall back to endpoint-agnostic map when endpoint unset (legacy files)
    if candidates and not candidates[0].endpoint:
        by_id = {t.id: t for t in current}
    safe, problems = [], []
    for c in candidates:
        now = by_id.get(c.id)
        if now is None:
            problems.append(f"{c.id}: gone before close; skip")
            continue
        if now.canonical != c.canonical:
            problems.append(
                f"{c.id}: navigated since approval "
                f"({redact_url(c.url)} -> {redact_url(now.url)}); skip")
            continue
        safe.append(c)
    return safe, problems


def diff_tab_sets(before: list[TabRecord],
                  after: list[TabRecord],
                  closed_ids: set[str]) -> dict:
    """Before/after set comparison (the 'verify rest stayed open' check)."""
    before_ids = {t.id for t in before}
    after_ids = {t.id for t in after}
    return {
        "closed_expected": sorted(closed_ids),
        "still_open_from_close_set": sorted(closed_ids & after_ids),
        "unexpectedly_closed": sorted((before_ids - after_ids) - closed_ids),
        "remaining": len(after_ids),
    }


# ---------------------------------------------------------------------------
# Atomic summary-file append (concurrency-safe)
# ---------------------------------------------------------------------------

def atomic_append(path: Path, lines: list[str]) -> None:
    """Append lines atomically: write-temp-in-same-dir + os.replace for the
    header-create step, then append under an exclusive sidecar lock so
    parallel subagents cannot interleave ('read -> edit -> count' races)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        # best-effort exclusive lock (POSIX flock; no-op on Windows)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            pass
        if not path.exists():
            tmp = tempfile.NamedTemporaryFile(
                "w", dir=str(path.parent), delete=False, encoding="utf-8")
            try:
                tmp.write("".join(lines))
                tmp.close()
                os.replace(tmp.name, str(path))
            except BaseException:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise
        else:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("".join(lines))
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def recount_entries(path: Path) -> int:
    """Authoritative recount of '## ' entries (fixes mutable batch counts)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    return sum(1 for line in text.splitlines() if line.startswith("## "))


# ---------------------------------------------------------------------------
# Firefox session helpers
# ---------------------------------------------------------------------------

MOZLZ4_MAGIC = b"mozLz40\0"


def copy_session_safe(src: Path, scratch: Path) -> Path:
    """Copy a live session file to scratch first; never read live in place.

    Enforces the documented 'copy first' invariant in code instead of
    relying on the agent remembering.
    """
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"not a file: {src}")
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    dst = scratch / (src.stem + ".copy" + src.suffix)
    shutil.copyfile(src, dst)
    return dst


def decode_mozlz4(raw: bytes) -> dict:
    """Decode mozLz4 payload. Requires `lz4` package (no brittle fallback)."""
    if raw[:8] != MOZLZ4_MAGIC:
        raise ValueError("not a mozilla lz4 session file")
    try:
        import lz4.block
    except ImportError as exc:
        raise RuntimeError(
            "the `lz4` python package is required "
            "(`pip install lz4`); the tail/lz4cat fallback was removed "
            "because it is known-unreliable") from exc
    return json.loads(lz4.block.decompress(raw[8:]))


def pick_current_entry(tab: dict) -> dict | None:
    """Pick the *selected* entry (index-aware), not blindly the last one.

    Firefox tabs keep navigation history in `entries` with a 1-based
    `index` (or `lastAccessed`). The visible tab is entries[index-1].
    """
    entries = tab.get("entries", []) or []
    if not entries:
        return None
    idx = tab.get("index", len(entries))
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        idx = len(entries)
    idx = max(1, min(idx, len(entries)))
    cand = entries[idx - 1]
    return cand if isinstance(cand, dict) else entries[-1]


def session_freshness(session_path: Path) -> dict:
    """Report staleness signals: mtime + whether a backup is newer."""
    p = Path(session_path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"exists": False}
    info: dict = {"exists": True, "mtime": mtime, "backup_newer": False}
    backups = p.parent / "sessionstore-backups"
    newest = 0.0
    if backups.is_dir():
        for f in backups.iterdir():
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
    info["backup_newer"] = newest > mtime
    return info
