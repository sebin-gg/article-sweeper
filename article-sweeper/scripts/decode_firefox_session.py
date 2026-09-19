"""Print live Firefox tab URLs and titles from sessionstore.jsonlz4.

Usage:
    python3 decode_firefox_session.py <sessionstore.jsonlz4>

Copies nothing. Reads the given path only, so pass a copy when the browser
is running. Prefers the `lz4` python package (`pip install lz4`) and falls
back to `tail` plus `lz4cat` on PATH. Tested against a Firefox
default-release profile.
"""
import json
import subprocess
import sys
from pathlib import Path


def check_path(path):
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"not a file: {path}")
    return p


def via_package(path):
    import lz4.block

    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:8] != b"mozLz40\0":
        raise SystemExit("not a mozilla lz4 session file")
    return json.loads(lz4.block.decompress(raw[8:]))


def via_cli(path):
    tail = subprocess.run(
        ["tail", "-c", "+9", path], capture_output=True, check=True,
        timeout=60,
    )
    raw = subprocess.run(
        ["lz4cat"], input=tail.stdout, capture_output=True, check=True,
        timeout=60,
    ).stdout
    start = raw.find(b'{"version"')
    if start < 0:
        raise SystemExit("no session JSON found in lz4cat output")
    end = raw.rfind(b"}")
    return json.loads(raw[start : end + 1])


def main(path):
    path = check_path(path)
    try:
        doc = via_package(path)
    except ImportError:
        try:
            doc = via_cli(path)
        except Exception as exc:
            raise SystemExit(
                f"lz4cat fallback failed ({exc}); pip install lz4 instead"
            ) from exc
    count = 0
    for window in doc.get("windows", []):
        for tab in window.get("tabs", []):
            entries = tab.get("entries", [])
            if not entries:
                continue
            last = entries[-1]
            print(f"{last.get('url')} | {last.get('title')}")
            count += 1
    print(f"TOTAL TABS: {count}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
