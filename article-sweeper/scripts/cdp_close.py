"""Close Chromium tabs by CDP id, with pre-close revalidation.

Usage:
    python3 cdp_close.py <ids.txt> [--port 9222] [--host 127.0.0.1]
        [--expect <cdp-before.json>] [--browser chrome] [--endpoint 127.0.0.1:9222]

Safety vs the old version:
- --port must satisfy 1 <= port <= 65535 (isdigit() alone is not enough).
- --host is restricted to loopback by design (documented trust boundary).
- With --expect, ids are revalidated against the fresh /json/list *right
  before* closing: ids that vanished or navigated (canonical-URL mismatch)
  are skipped, never closed blind.
- After closing, each target is re-fetched to confirm it disappeared;
  a bare HTTP 200 is NOT treated as proof of close.

ids.txt holds one tab id per line. Prints per-id results to stdout and a
summary to stderr. Exits 0 only when every requested id either closed or
was safely skipped as gone (skips are reported, not hidden).
"""
import argparse
import concurrent.futures
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_lib import (  # noqa: E402
    DEFAULT_HOST,
    TabRecord,
    parse_cdp_list,
    redact_url,
    validate_host,
    validate_port,
    verify_close_candidates,
)


def fetch_list(host, port, timeout=5):
    url = f"http://{host}:{port}/json/list"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def close_one(host, port, tab_id, timeout=5):
    url = f"http://{host}:{port}/json/close/{urllib.parse.quote(tab_id, safe='')}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, method="PUT"), timeout=timeout
        ) as resp:
            body = redact_url(resp.read().decode("utf-8", "replace"))[:80]
        # verify the target actually disappeared (not just HTTP 200)
        try:
            live = {d.get("id") for d in fetch_list(host, port)
                    if isinstance(d, dict)}
        except Exception:
            live = None  # endpoint unreachable post-close: report honestly
        if live is None:
            return tab_id, True, f"{body} (unverified: list unreachable)"
        if tab_id in live:
            return tab_id, False, "close returned 200 but target still listed"
        return tab_id, True, body
    except Exception as exc:
        return tab_id, False, str(exc)[:120]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("ids_file")
    ap.add_argument("--port", default="9222")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--expect", default="",
                    help="CDP /json/list dump for pre-close revalidation")
    ap.add_argument("--browser", default="")
    ap.add_argument("--endpoint", default="")
    args = ap.parse_args(argv)

    try:
        port = validate_port(args.port)
        host = validate_host(args.host)
    except ValueError as exc:
        raise SystemExit(str(exc))

    ids_path = Path(args.ids_file)
    if not ids_path.is_file():
        raise SystemExit(f"not a file: {args.ids_file}")
    with open(ids_path, encoding="utf-8") as fh:
        ids = [ln.strip() for ln in fh if ln.strip()]
    if not ids:
        raise SystemExit("no tab ids in file; refusing empty close run")
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate ids in file; fix the close list first")

    endpoint = args.endpoint or f"{host}:{port}"
    if args.expect:
        ep = Path(args.expect)
        if not ep.is_file():
            raise SystemExit(f"not a file: {args.expect}")
        with open(ep, encoding="utf-8") as fh:
            before = parse_cdp_list(json.load(fh), endpoint=endpoint,
                                    browser=args.browser)
        # refresh live list for the identity check
        try:
            live_raw = fetch_list(host, port)
            live = parse_cdp_list(live_raw, endpoint=endpoint,
                                  browser=args.browser)
        except Exception as exc:
            raise SystemExit(f"cannot revalidate before close: {exc}")
        cand_by_id = {t.id: t for t in before}
        missing = [i for i in ids if i not in cand_by_id]
        if missing:
            raise SystemExit(
                f"refusing close: {len(missing)} id(s) not in --expect dump "
                f"(stale/forged close list?): {missing[:5]}")
        candidates = [cand_by_id[i] for i in ids]
        safe, problems = verify_close_candidates(candidates, live)
        for prob in problems:
            print(f"SKIP {prob}")
        if len(safe) != len(candidates):
            print(f"revalidation skipped {len(candidates) - len(safe)}/"
                  f"{len(candidates)}", file=sys.stderr)
        ids = [t.id for t in safe]
        if not ids:
            print("nothing safe to close after revalidation", file=sys.stderr)
            sys.exit(0)

    ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        for tab_id, done, msg in pool.map(
                lambda i: close_one(host, port, i), ids):
            print(f"{'CLOSED' if done else 'FAILED'} {tab_id} {msg[:80]}")
            ok += done
    print(f"closed ok: {ok}/{len(ids)}", file=sys.stderr)
    sys.exit(0 if ok == len(ids) else 1)


if __name__ == "__main__":
    main()
