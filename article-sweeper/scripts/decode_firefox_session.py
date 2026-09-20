"""Print live Firefox tab URLs and titles from sessionstore.jsonlz4.

Usage:
    python3 decode_firefox_session.py <sessionstore.jsonlz4>
        [--scratch DIR] [--no-copy] [--redact]

Safety model (read-only enumeration; Firefox close is manual):
- By default the script COPIES the session file to a temp/scratch dir and
  decodes the copy, so the live file is never read in place while Firefox
  may be writing it. Pass --no-copy only when you already hand it a copy.
- Prints staleness warnings: file mtime plus whether a file under
  sessionstore-backups/ is newer (backup may hold fresher state).
- Uses the tab's selected entry (index-aware), not blindly entries[-1].
- Requires the `lz4` python package (`pip install lz4`). The old
  tail/lz4cat fallback was removed: it was documented-unreliable and
  must not be part of the supported flow.
- With --redact, token-like query values are masked in printed URLs.
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_lib import (  # noqa: E402
    copy_session_safe,
    decode_mozlz4,
    pick_current_entry,
    redact_url,
    session_freshness,
)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("session_path")
    ap.add_argument("--scratch", default="",
                    help="dir for the safe copy (default: system temp)")
    ap.add_argument("--no-copy", action="store_true",
                    help="input is already a copy; decode in place")
    ap.add_argument("--redact", action="store_true")
    args = ap.parse_args(argv)

    src = Path(args.session_path)
    if not src.is_file():
        raise SystemExit(f"not a file: {args.session_path}")

    info = session_freshness(src)
    if info.get("backup_newer"):
        print("WARNING: a sessionstore-backups file is NEWER than "
              "sessionstore.jsonlz4; state may be stale/incomplete. "
              "Firefox may still be running or may have crashed; "
              "treat output as a snapshot, not live ground truth.",
              file=sys.stderr)
    try:
        age = time.time() - info.get("mtime", time.time())
        print(f"session mtime age: {age:.0f}s", file=sys.stderr)
    except Exception:
        pass

    if args.no_copy:
        work = src
    else:
        scratch = Path(args.scratch) if args.scratch else Path(
            tempfile.gettempdir()) / "opencode"
        work = copy_session_safe(src, scratch)
        print(f"decoded safe copy: {work} (live file untouched)",
              file=sys.stderr)

    try:
        raw = work.read_bytes()
    except OSError as exc:
        raise SystemExit(f"cannot read {work}: {exc}")
    try:
        doc = decode_mozlz4(raw)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    except ValueError as exc:
        raise SystemExit(str(exc))

    count = 0
    for window in doc.get("windows", []) or []:
        for tab in window.get("tabs", []) or []:
            if not isinstance(tab, dict):
                continue
            cur = pick_current_entry(tab)
            if not cur:
                continue
            url = cur.get("url", "") or ""
            title = cur.get("title", "") or ""
            if args.redact:
                url = redact_url(url)
            print(f"{url} | {title}")
            count += 1
    print(f"TOTAL TABS: {count}", file=sys.stderr)
    print("NOTE: Firefox enumeration is read-only; "
          "close summarized tabs by hand from this list.", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
