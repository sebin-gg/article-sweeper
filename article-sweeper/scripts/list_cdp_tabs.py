"""List live Chromium page tabs from a CDP /json/list dump.

Usage:
    python3 list_cdp_tabs.py <cdp.json> [--endpoint 127.0.0.1:9224]
        [--browser chrome] [--redact]

Prints one line per validated page tab (internal targets excluded by CDP
type + scheme, not by substring):
    <id> | <title> | <url>
With --redact, sensitive query values are masked for logs.
Endpoint/browser identity is printed to stderr so a bare tab id is never
ambiguous when several browsers expose debugging at once.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_lib import parse_cdp_list, redact_url  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cdp_json")
    ap.add_argument("--endpoint", default="")
    ap.add_argument("--browser", default="")
    ap.add_argument("--redact", action="store_true",
                    help="mask token-like query params in printed URLs")
    args = ap.parse_args(argv)
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
