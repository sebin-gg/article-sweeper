"""Close Chromium tabs by CDP id, with mandatory pre-close revalidation.

Usage:
    python3 cdp_close.py <ids.txt> --expect <cdp-before.json>
        --browser chrome
        [--port 9222] [--host 127.0.0.1] [--endpoint 127.0.0.1:9222]

Safety (all mandatory, no bypass):
- --expect is REQUIRED: ids are revalidated against the fresh /json/list
  *right before* closing. Ids that vanished or navigated
  (canonical-URL mismatch) are skipped, never closed blind. Ids missing
  from --expect abort the run (stale/forged close list).
- --browser is REQUIRED: the /json/version product string must identify
  that browser (vendor aliases handled, e.g. Edge=`Edg/`, Opera=`OPR/`),
  so a stray local CDP service on a reused port cannot be driven by
  mistake. There is no "some Chromium endpoint" mode.
- After closing, each target is re-fetched to confirm it disappeared;
  a bare HTTP 200 is NOT proof of close. Unverifiable closes (list
  unreachable post-close) are reported FAILED with non-zero exit.
- --port must satisfy 1 <= port <= 65535. --host is loopback-only.

ids.txt holds one tab id per line. Prints per-id results to stdout and a
summary to stderr. Exits 0 only when every requested id either closed
(verified disappearance) or was safely skipped as gone.
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
    cdp_url,
    check_endpoint_identity,
    parse_cdp_list,
    redact_url,
    validate_host,
    validate_port,
    verify_close_candidates,
)


def fetch_list(host, port, timeout=5):
    url = cdp_url(host, port, "/json/list")  # NOSONAR python:S5332
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def close_one(host, port, tab_id, timeout=5):
    url = cdp_url(host, port,  # NOSONAR python:S5332
                  f"/json/close/{urllib.parse.quote(tab_id, safe='')}")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, method="PUT"), timeout=timeout
        ) as resp:
            body = redact_url(resp.read().decode("utf-8", "replace"))[:80]
        # verify the target actually disappeared (not just HTTP 200).
        # Unverifiable == FAILED: a safety tool must not claim success
        # it could not prove.
        try:
            live = {d.get("id") for d in fetch_list(host, port)
                    if isinstance(d, dict)}
        except Exception as exc:
            return tab_id, False, f"{body} (UNVERIFIED: list unreachable: {exc})".strip()[:120]
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
    ap.add_argument("--expect", default="", required=True,
                    help="REQUIRED: CDP /json/list dump for pre-close revalidation")
    ap.add_argument("--browser", default="", required=True,
                    help="REQUIRED: expected browser for /json/version identity")
    ap.add_argument("--endpoint", default="")
    args = ap.parse_args(argv)

    try:
        port = validate_port(args.port)
        host = validate_host(args.host)
    except ValueError as exc:
        raise SystemExit(str(exc))

    if not args.expect:
        raise SystemExit("refusing close: --expect is required (no blind close by id)")

    # Validate endpoint identity before touching any tab: a reused port
    # may host a different CDP service. --browser is mandatory and the
    # product check is strict (vendor aliases handled in sweep_lib).
    try:
        check_endpoint_identity(host, port, expect_browser=args.browser)
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

    # Post-close: diff FRESH before-close baseline vs final. --expect is
    # the authorization snapshot (stale by design); `live` is the actual
    # pre-close state. Unrelated tabs that closed naturally between
    # --expect and revalidation must not count as unexpected closures.
    from sweep_lib import diff_tab_sets as _diff
    live_by_id = {t.id: t for t in live}

    ok = 0
    closed_ids: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        for tab_id, done, msg in pool.map(
                lambda i: close_one(host, port, i), ids):
            print(f"{'CLOSED' if done else 'FAILED'} {tab_id} {msg[:80]}")
            ok += done
            if done:
                closed_ids.append(tab_id)
    print(f"closed ok: {ok}/{len(ids)}", file=sys.stderr)
    # Before/after set comparison: anything from the close set still open
    # or anything outside it that vanished => failure.
    try:
        after_raw = fetch_list(host, port)
        after = parse_cdp_list(after_raw, endpoint=endpoint,
                               browser=args.browser)
        diff = _diff(list(live_by_id.values()), after, set(closed_ids))
        if diff["still_open_from_close_set"]:
            print(f"FAILED still open: {diff['still_open_from_close_set'][:5]}",
                  file=sys.stderr)
        if diff["unexpectedly_closed"]:
            print(f"FAILED unexpectedly closed: {diff['unexpectedly_closed'][:5]}",
                  file=sys.stderr)
        if diff["still_open_from_close_set"] or diff["unexpectedly_closed"]:
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"FAILED post-close verification unreachable: {exc}",
              file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if ok == len(ids) else 1)


if __name__ == "__main__":
    main()
