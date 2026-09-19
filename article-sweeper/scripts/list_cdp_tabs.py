"""List live Chromium page tabs from a CDP /json/list dump.

Usage:
    python3 list_cdp_tabs.py <cdp.json>

Prints one line per page tab (skips devtools:// targets):
    <id> | <title> | <url>
"""
import json
import sys
from pathlib import Path


def main(path):
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"not a file: {path}")
    with open(p, encoding="utf-8") as fh:
        data = json.load(fh)
    pages = [
        d for d in data
        if d.get("type") == "page"
        and "devtools://" not in d.get("url", "")
    ]
    for d in pages:
        print(f"{d.get('id')} | {d.get('title', '')} | {d.get('url', '')}")
    print(f"TOTAL PAGES: {len(pages)}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
