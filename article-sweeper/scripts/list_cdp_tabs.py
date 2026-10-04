"""List live Chromium page tabs from a CDP /json/list dump.

Usage:
    python3 list_cdp_tabs.py <cdp.json> [--endpoint 127.0.0.1:9224]
        [--browser chrome] [--redact]
        [--host 127.0.0.1 --port 9224 --check-endpoint]

Prints one line per validated page tab (internal targets excluded by CDP
type + scheme, not by substring):
    <id> | <title> | <url>
With --redact, sensitive query values are masked for logs.
Endpoint/browser identity is printed to stderr so a bare tab id is never
ambiguous when several browsers expose debugging at once.
With --check-endpoint (+ --host/--port/--browser, all required),
/json/version is checked BEFORE enumeration output so a reused/wrong
local port cannot lead to summarizing the wrong browser's tabs.
--browser is mandatory there: attesting "some Chromium endpoint"
without the expected identity is not allowed.
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_lib import (  # noqa: E402
    confirm_endpoint,
    parse_cdp_list,
    DEFAULT_HOST,
    cdp_url,
    wait_for_stable_tabs,
    STABLE_MIN_TABS,
    STABLE_TIMEOUT_SECONDS,
    STABLE_SETTLE_SECONDS,
    redact_url,
    validate_port,
)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cdp_json", nargs="?",
                    help="a /json/list dump; omit when --wait-stable is used "
                         "and the list is fetched live until it settles")
    ap.add_argument("--wait-stable", action="store_true",
                    help="poll the live endpoint until the tab set stops "
                         "changing, then list it. Session restore re-creates "
                         "tabs asynchronously; enumerating during that window "
                         "captures a stable but WRONG baseline.")
    ap.add_argument("--settle", type=float, default=STABLE_SETTLE_SECONDS,
                    help="seconds an unchanged tab set must hold (default "
                         f"{STABLE_SETTLE_SECONDS}; shorter below "
                         f"{STABLE_MIN_TABS} tabs)")
    ap.add_argument("--wait-timeout", type=float,
                    default=STABLE_TIMEOUT_SECONDS,
                    help="give up waiting after this many seconds (default "
                         f"{STABLE_TIMEOUT_SECONDS})")
    ap.add_argument("--endpoint", default="")
    ap.add_argument("--browser", default="")
    ap.add_argument("--redact", action="store_true",
                    help="mask token-like query params in printed URLs")
    ap.add_argument("--host", default="")
    ap.add_argument("--port", default="")
    ap.add_argument("--check-endpoint", action="store_true",
                    help="validate /json/version identity before printing")
    ap.add_argument("--expect-cmd", default="",
                    help="with --check-endpoint: command-line fragment the "
                         "listening process must show; Linux /proc cmdline "
                        "or Windows image path")
    args = ap.parse_args(argv)
    if args.wait_stable:
        if not args.cdp_json and not (args.host and args.port):
            ap.error("--wait-stable needs a dump file, or --host and --port")
        if not args.cdp_json:
            host = args.host or DEFAULT_HOST
            port = validate_port(args.port)
            endpoint = args.endpoint or f"{host}:{port}"

            def fetch():
                url = cdp_url(host, port, "/json/list")  # NOSONAR python:S5332
                with urllib.request.urlopen(url, timeout=5) as resp:
                    return json.loads(resp.read().decode("utf-8", "replace"))

            pages, info = wait_for_stable_tabs(
                fetch, endpoint=endpoint, browser=args.browser,
                settle_seconds=args.settle, timeout=args.wait_timeout)
            print(f"# waited {info['waited']}s over {info['samples']} samples "
                  f"({info['changes']} change(s)); stable={info['stable']} "
                  f"{info['reason']}", file=sys.stderr)
            if not info["stable"]:
                print("# WARNING: the tab set never settled. This baseline may "
                      "have been captured mid session-restore. Re-run, or "
                      "accept knowingly.", file=sys.stderr)
            for r in pages:
                url = redact_url(r.url) if args.redact else r.url
                print(f"{r.id} | {r.title} | {url}")
            print(f"TOTAL PAGES: {len(pages)}", file=sys.stderr)
            if endpoint or args.browser:
                print(f"ENDPOINT: {endpoint or '?'} "
                      f"BROWSER: {args.browser or '?'}", file=sys.stderr)
            return 0

    if args.check_endpoint:
        if not args.host or not args.port:
            raise SystemExit("--check-endpoint needs --host and --port")
        if not args.browser:
            raise SystemExit("--check-endpoint needs --browser: refusing to "
                             "attest an endpoint without the expected "
                             "browser identity")
        try:
            _, owner, _ = confirm_endpoint(
                args.host, validate_port(args.port),
                expect_browser=args.browser,
                expect_cmd=args.expect_cmd)
        except ValueError as exc:
            raise SystemExit(str(exc))
        if owner is not None:
            print(f"endpoint owner pid: {owner}", file=sys.stderr)
    # Trust boundary: local-operator CLI tool — argv path comes from the
    # invoking operator (same trust as shell redirection), validated
    # (is-file check) before reading. NOSONAR marks a reviewed S8707.
    p = Path(args.cdp_json)
    if not p.is_file():
        raise SystemExit(f"not a file: {args.cdp_json}")
    with open(p, encoding="utf-8") as fh:  # NOSONAR pythonsecurity:S8707
        data = json.load(fh)
    try:
        pages = parse_cdp_list(data, endpoint=args.endpoint,
                               browser=args.browser)
    except ValueError as exc:
        raise SystemExit(f"invalid CDP payload: {exc}")
    for t in pages:
        url = redact_url(t.url) if args.redact else t.url
        print(f"{t.id} | {t.title} | {url}")
    print(f"TOTAL PAGES: {len(pages)}", file=sys.stderr)
    if args.endpoint or args.browser:
        print(f"ENDPOINT: {args.endpoint or '?'} "
              f"BROWSER: {args.browser or '?'}", file=sys.stderr)


if __name__ == "__main__":
    main()
