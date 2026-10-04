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
  mistake. There is no "some Chromium endpoint" mode. Vendor-blind
  products (Thorium reports plain "Chrome/...") are acceptable ONLY
  when --expect-cmd also proves the listening process: the product must
  fail to self-identify AND the process must match, or the run aborts.
- After closing, each target is re-fetched to confirm it disappeared;
  a bare HTTP 200 is NOT proof of close. Unverifiable closes (list
  unreachable post-close) are reported FAILED with non-zero exit.
- Closing what would leave the browser with zero page tabs is refused
  by default (verified live: Thorium exits the whole browser);
  --allow-last-tab opts in explicitly.
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
from cdp_forms import probe_dirty_form  # noqa: E402
from sweep_lib import (  # noqa: E402
    DEFAULT_HOST,
    TabRecord,
    cdp_url,
    confirm_endpoint,
    endpoint_gone_confirmed,
    is_never_close,
    last_page_guard,
    parse_cdp_list,
    parse_summary_file,
    redact_url,
    validate_host,
    validate_port,
    verify_close_candidates,
)


def fetch_list(host, port, timeout=5):
    url = cdp_url(host, port, "/json/list")  # NOSONAR python:S5332
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def close_one(host, port, tab_id, timeout=5, *, expect_canonical=None,
              expect_url="", endpoint="", browser=""):
    """Close one tab, re-validating its identity immediately beforehand.

    The up-front `verify_close_candidates()` pass only covers the instant it
    ran. Every tab closed after that -- and with a 20-wide pool on a 50-tab
    sweep the last one fires many seconds later -- is closing a stale
    decision. So the id/URL pair is re-checked here, in the last possible
    moment, and only then is the PUT issued.

    `expect_canonical=None` keeps the legacy behaviour for callers that have
    no expectation to check.
    """
    if expect_canonical is not None:
        try:
            live = parse_cdp_list(fetch_list(host, port, timeout),
                                  endpoint=endpoint, browser=browser)
        except Exception as exc:  # noqa: BLE001 - unverifiable must not close
            return tab_id, False, (f"SKIP: cannot revalidate before close: "
                                   f"{type(exc).__name__}")[:120]
        now = {t.id: t for t in live}
        rec = now.get(tab_id)
        if rec is None:
            return tab_id, False, "SKIP: target gone before close"
        if rec.canonical != expect_canonical:
            return tab_id, False, (
                f"SKIP: navigated before close ({redact_url(expect_url)} -> "
                f"{redact_url(rec.url)})")[:120]

    url = cdp_url(host, port,  # NOSONAR python:S5332
                  f"/json/close/{urllib.parse.quote(tab_id, safe='')}")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, method="PUT"), timeout=timeout
        ) as resp:
            body = redact_url(resp.read().decode("utf-8", "replace"))[:80]
        # verify the target actually disappeared (not just HTTP 200).
        # Some browsers apply the close asynchronously ("Target is closing"
        # while still listed), so poll briefly before calling it failed.
        # Unverifiable == FAILED: a safety tool must not claim success
        # it could not prove. Exception: the endpoint connection-REFUSED
        # means the browser process itself is provably gone (last-tab
        # exit) — the close definitely applied.
        import time as _time
        live = None
        last_err = ""
        for _ in range(12):  # ~3s total
            try:
                live = {d.get("id") for d in fetch_list(host, port)
                        if isinstance(d, dict)}
            except Exception as exc:
                last_err = str(exc)[:80]
                if endpoint_gone_confirmed(host, port) in (
                        "refused", "dead"):
                    return tab_id, True, "closed (endpoint gone: browser "\
                        "exited after last-tab close)"
                live = None
                break  # unreachable: report below, no point polling
            if tab_id not in live:
                break
            _time.sleep(0.25)
        if live is None:
            return tab_id, False, f"{body} (UNVERIFIED: list unreachable: {last_err})".strip()[:120]
        if tab_id in live:
            return tab_id, False, "close returned 200 but target still listed"
        return tab_id, True, body
    except Exception as exc:
        return tab_id, False, str(exc)[:120]


def _probe_dirty_forms(tabs, host, timeout=5.0):
    """Probe each tab for unsaved input, in parallel. Returns {id: reason}.

    Fail-closed: a tab we cannot verify is treated as dirty, so an unreachable
    or unsupported target is skipped rather than closed on a guess.
    """
    if not tabs:
        return {}
    out: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(probe_dirty_form, t.ws_url, timeout=timeout): t
                for t in tabs}
        for fut in concurrent.futures.as_completed(futs):
            tab = futs[fut]
            try:
                dirty, why = fut.result()
            except Exception as exc:  # noqa: BLE001 - unverifiable => skip
                dirty, why = True, f"probe-crashed:{type(exc).__name__}"
            if dirty:
                out[tab.id] = why
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("ids_file")
    ap.add_argument("--port", default="9222")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--summary", default="", required=True,
                    help="summary file; a tab closes ONLY if its canonical "
                         "URL has a schema-valid entry here")
    ap.add_argument("--expect", default="", required=True,
                    help="REQUIRED: CDP /json/list dump for pre-close revalidation")
    ap.add_argument("--browser", default="", required=True,
                    help="REQUIRED: expected browser for /json/version identity")
    ap.add_argument("--endpoint", default="")
    ap.add_argument("--expect-cmd", default="",
                    help="optional: command-line fragment (binary name or "
                         "--user-data-dir) the listening process must show; "
                         "Linux /proc cmdline or Windows image path; fails "
                         "closed when unsupported")
    ap.add_argument("--no-form-guard", action="store_true",
                    help="skip the dirty-form probe. The probe fails CLOSED, so "
                         "on a browser that does not expose websocket URLs "
                         "this will block every close unless opted out.")
    ap.add_argument("--allow-last-tab", action="store_true",
                    help="permit closing what would leave zero page tabs "
                         "(verified live: Thorium exits the whole browser); "
                         "default refuses")
    args = ap.parse_args(argv)

    try:
        port = validate_port(args.port)
        host = validate_host(args.host)
    except ValueError as exc:
        raise SystemExit(str(exc))

    if not args.expect:
        raise SystemExit("refusing close: --expect is required (no blind close by id)")

    # Validate endpoint identity before touching any tab: a reused port
    # may host a different CDP service. --browser is mandatory. Product-
    # blind vendors (Thorium = plain "Chrome/...") may proceed ONLY when
    # --expect-cmd proves the listening process; see sweep_lib.
    # confirm_endpoint for the exact rules.
    try:
        ver, owner, _ = confirm_endpoint(
            host, port, expect_browser=args.browser,
            expect_cmd=args.expect_cmd)
    except ValueError as exc:
        msg = str(exc)
        if not args.expect_cmd and "reports Browser" in msg:
            msg += ("\nif this endpoint is genuinely " + args.browser +
                    " (e.g. Thorium reports plain 'Chrome/...'), rerun "
                    "with --expect-cmd <binary-name> for a "
                    "process-ownership proof")
        raise SystemExit(msg)
    if owner is not None:
        print(f"endpoint owner pid: {owner}", file=sys.stderr)
    # Ambiguity notice: CDP cannot distinguish real Chrome/Chromium from
    # vendor-blind forks (Thorium/Brave report plain 'Chrome/<v>'). When
    # --browser chrome|chromium passes without a process proof, say so
    # instead of silently asserting an identity CDP cannot prove.
    if (args.browser in ("chrome", "chromium") and not args.expect_cmd
            and ver.get("Browser", "").startswith("Chrome/")):
        print("note: 'Chrome/...' product strings are shared by "
              "vendor-blind forks; add --expect-cmd to prove the "
              "listening process identity", file=sys.stderr)

    # Trust boundary: local-operator CLI tool. argv paths come from the
    # invoking operator (same trust as shell redirection) and are
    # validated (is-file checks above) before reading. The NOSONAR marks
    # below record reviewed S8707 findings, not blind suppression.
    ids_path = Path(args.ids_file)
    if not ids_path.is_file():
        raise SystemExit(f"not a file: {args.ids_file}")
    with open(ids_path, encoding="utf-8") as fh:  # NOSONAR pythonsecurity:S8707
        ids = [ln.strip() for ln in fh if ln.strip()]
    if not ids:
        raise SystemExit("no tab ids in file; refusing empty close run")
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate ids in file; fix the close list first")

    endpoint = args.endpoint or f"{host}:{port}"
    ep = Path(args.expect)
    if not ep.is_file():
        raise SystemExit(f"not a file: {args.expect}")
    with open(ep, encoding="utf-8") as fh:  # NOSONAR pythonsecurity:S8707
        before = parse_cdp_list(json.load(fh), endpoint=endpoint,
                                browser=args.browser)
    # refresh live list for the identity check
    try:
        live_raw = fetch_list(host, port)
        live = parse_cdp_list(live_raw, endpoint=endpoint,
                              browser=args.browser)
    except Exception as exc:
        raise SystemExit(f"cannot revalidate before close: {exc}")
    # Close gate: an entry must EXIST in the summary file and be
    # schema-valid. Without this, "closed but never summarized" is prevented
    # only by the agent's discipline; with it, the summary file is the
    # authorization record and the run audits itself -- every close is backed
    # by an entry, and every entry is one you can read.
    sindex, sproblems = parse_summary_file(Path(args.summary))
    for prob in sproblems:
        print(f"SUMMARY-PROBLEM {prob}", file=sys.stderr)
    if not sindex:
        raise SystemExit(
            "refusing close: summary file has no schema-valid entries "
            f"({len(sproblems)} problem(s)); see SUMMARY-PROBLEM above")

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
    # Apply the summary gate per tab. A tab whose canonical URL has no
    # schema-valid entry is not closable, no matter how it got into the close
    # set. Matching is on the canonical URL, so a Link: written in a cleaner
    # form than the tab's own URL still matches.
    # Hard never-close blocklist. This runs BEFORE and independently of the
    # summary gate: a tab on an app route (checkout, 2FA prompt, an edit form
    # with unsaved input) is never closed, even when it has a schema-valid
    # summary entry and the classifier called it an article. Losing a half-
    # filled form is the one loss a session backup cannot undo.
    app_routes = [t for t in safe if is_never_close(t.url)[0]]
    for t in app_routes:
        print(f"SKIP {t.id}: {is_never_close(t.url)[1]} "
              f"({redact_url(t.url)})")
    if app_routes:
        print(f"never-close blocklist skipped {len(app_routes)}/{len(candidates)}",
              file=sys.stderr)
    safe = [t for t in safe if not is_never_close(t.url)[0]]
    if not safe:
        print("nothing safe to close: every candidate is a protected app route",
              file=sys.stderr)
        sys.exit(0)

    # Dirty-form guard. A tab holding unsaved user input is the one loss a
    # session backup cannot restore, and that state lives inside the page --
    # the URL and target id look perfectly normal while a half-typed reply is
    # about to be destroyed. One Runtime.evaluate per close candidate, in
    # parallel, via the same revalidated snapshot.
    if not args.no_form_guard:
        dirty_ids = _probe_dirty_forms(safe, host)
        if dirty_ids:
            for tab_id, why in sorted(dirty_ids.items()):
                print(f"SKIP {tab_id}: {why}")
            print(f"dirty-form guard skipped {len(dirty_ids)}/{len(safe)}",
                  file=sys.stderr)
        safe = [t for t in safe if t.id not in dirty_ids]
        if not safe:
            print("nothing safe to close: every candidate holds unsaved input",
                  file=sys.stderr)
            sys.exit(0)

    unsummarized = [t for t in safe if t.canonical not in sindex]
    for t in unsummarized:
        print(f"SKIP {t.id}: no schema-valid summary entry for "
              f"{redact_url(t.url)}")
    if unsummarized:
        print(f"summary gate skipped {len(unsummarized)}/{len(candidates)}",
              file=sys.stderr)
    safe = [t for t in safe if t.canonical in sindex]
    if not safe:
        print("nothing safe to close: no schema-valid summary entry matches "
              "the requested tabs", file=sys.stderr)
        sys.exit(0)
    ids = [t.id for t in safe]
    if not ids:
        print("nothing safe to close after revalidation", file=sys.stderr)
        sys.exit(0)

    # Last-page guard: closing every safe candidate would leave the
    # browser with zero page tabs -> verified live (Thorium) this exits
    # the whole browser. Refuse by default; --allow-last-tab opts in.
    # Post-close: diff FRESH before-close baseline vs final. --expect is
    # the authorization snapshot (stale by design); `live` is the actual
    # pre-close state. Unrelated tabs that closed naturally between
    # --expect and revalidation must not count as unexpected closures.
    from sweep_lib import diff_tab_sets as _diff
    live_by_id = {t.id: t for t in live}

    # Last-page guard: closing every safe candidate would leave the
    # browser with zero page tabs -> verified live (Thorium) this exits
    # the whole browser. Refuse by default; --allow-last-tab opts in.
    if not args.allow_last_tab:
        try:
            last_page_guard(safe, list(live_by_id.values()))
        except ValueError as exc:
            raise SystemExit(str(exc))

    ok = 0
    closed_ids: list[str] = []
    # Each close carries the canonical URL it was approved for, so the check
    # happens per-tab at close time rather than once for the whole batch.
    safe_by_id = {t.id: t for t in safe}

    def _close(i):
        rec = safe_by_id[i]
        return close_one(host, port, i, expect_canonical=rec.canonical,
                         expect_url=rec.url, endpoint=endpoint,
                         browser=args.browser)

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        for tab_id, done, msg in pool.map(_close, ids):
            print(f"{'CLOSED' if done else 'FAILED'} {tab_id} {msg[:80]}")
            ok += done
            if done:
                closed_ids.append(tab_id)
    print(f"closed ok: {ok}/{len(ids)}", file=sys.stderr)
    # Before/after set comparison: anything from the close set still open
    # or anything outside it that vanished => failure.
    # --allow-last-tab exception: when every close applied and the
    # endpoint then disappears (connection refused/unreachable), that is
    # the verified behavior of a browser exiting on its last page close
    # (Thorium) - the closes succeeded and there is nothing left to
    # verify. Report and exit 0. Any other failure stays a failure.
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
        gone = (endpoint_gone_confirmed(host, port)
                if args.allow_last_tab and closed_ids
                and ok == len(ids) else None)
        if gone in ("refused", "dead"):
            print(f"allowed last-tab shutdown: endpoint {gone} after "
                  f"close ({exc}) - expected browser exit, closes "
                  f"verified", file=sys.stderr)
            sys.exit(0)
        if gone == "alive":
            print(f"FAILED endpoint answering again but list unreadable: "
                  f"{exc}", file=sys.stderr)
            sys.exit(1)
        print(f"FAILED post-close verification unreachable: {exc}",
              file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if ok == len(ids) else 1)
    # endpoint_gone_confirmed classifications used above: "refused" and
    # "dead" both mean the browser process is gone (proof vs strong
    # evidence); "alive" or None fail closed.


if __name__ == "__main__":
    main()
