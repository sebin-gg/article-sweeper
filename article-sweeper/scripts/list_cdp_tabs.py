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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_lib import (  # noqa: E402
    check_endpoint_identity,
    parse_cdp_list,
    redact_url,
    validate_port,
    verify_endpoint_process,
)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cdp_json")
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
                         "listening process must show (Linux /proc)")
    args = ap.parse_args(argv)
    if args.check_endpoint:
        if not args.host or not args.port:
            raise SystemExit("--check-endpoint needs --host and --port")
        if not args.browser:
            raise SystemExit("--check-endpoint needs --browser: refusing to "
                             "attest an endpoint without the expected "
                             "browser identity")
        try:
            check_endpoint_identity(args.host, args.port,
                                    expect_browser=args.browser)
        except ValueError as exc:
            raise SystemExit(str(exc))
        if args.expect_cmd:
            try:
                owner = verify_endpoint_process(
                    validate_port(args.port), args.expect_cmd)
            except (ValueError, RuntimeError) as exc:
                raise SystemExit(f"endpoint process check failed: {exc}")
            print(f"endpoint owner pid: {owner}", file=sys.stderr)
    p = Path(args.cdp_json)
    if not p.is_file():
        raise SystemExit(f"not a file: {args.cdp_json}")
    with open(p, encoding="utf-8") as fh:
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
