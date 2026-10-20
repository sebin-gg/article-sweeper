"""Print live Firefox tab URLs and titles from sessionstore.jsonlz4.

Usage:
    python3 decode_firefox_session.py <sessionstore.jsonlz4>
        [--scratch DIR] [--no-copy] [--redact] [--keep-copy]

Safety model (read-only enumeration; Firefox close is manual):
- By default the script COPIES the session file to a temp/scratch dir and
  decodes the copy (stable-snapshot retry: two identical reads required),
  so the live file is never read in place while Firefox may be writing
  it. The scratch copy is DELETED at the end of the run (success or
  failure) unless --keep-copy is given.
- --no-copy is ONLY for inputs that already are copies: the path must
  live under the scratch/temp dir or carry a .copy. marker, otherwise
  the script refuses (protects against accidentally pointing it at the
  live profile file).
- Prints staleness warnings: file mtime plus whether a file under
  sessionstore-backups/ is newer (backup may hold fresher state).
- Uses the tab's selected entry (index-aware), not blindly entries[-1].
- Requires the `lz4` python package (`pip install lz4`). The old
  tail/lz4cat fallback was removed: it was documented-unreliable and
  must not be part of the supported flow.
- With --redact, token-like values are masked in printed URLs (query,
  fragment, userinfo, token-shaped path segments). Redaction is
  best-effort: prefer it plus scratch cleanup, never treat a redacted
  URL as proven-safe to share.
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_lib import (  # noqa: E402
    cleanup_session_copy,
    copy_session_safe,
    decode_mozlz4,
    pick_current_entry,
    redact_url,
    session_freshness,
)


def _looks_like_copy(path: Path, scratch_roots: list[Path] | None = None) -> bool:
    """Decide whether --no-copy input is plausibly already a scratch copy.

    Accepts paths that EITHER carry the `.copy.` naming marker (written by
    copy_session_safe) OR resolve inside a known scratch root (the default
    `<tempdir>/opencode` dir or an explicit `--scratch` dir), checked with
    directory-boundary containment — never a substring test, so a live
    profile path that merely contains the text "opencode" is refused.
    """
    name = path.name
    if ".copy." in name or name.endswith(".copy"):
        return True
    roots = list(scratch_roots or [])
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except (OSError, ValueError, RuntimeError):
            continue
    return False


def _no_copy_refused(src: Path, scratch_arg: str) -> bool:
    """True when --no-copy input must be refused (not a plausible copy)."""
    roots = [Path(tempfile.gettempdir()) / "opencode"]
    if scratch_arg:
        roots.append(Path(scratch_arg))
    return not _looks_like_copy(src, roots)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("session_path")
    ap.add_argument("--scratch", default="",
                    help="dir for the safe copy (default: system temp)")
    ap.add_argument("--no-copy", action="store_true",
                    help="input is already a scratch copy; decode in place "
                         "(path must look like a copy, else refused)")
    ap.add_argument("--redact", action="store_true")
    ap.add_argument("--keep-copy", action="store_true",
                    help="do not delete the scratch copy at end of run")
    args = ap.parse_args(argv)

    src = Path(args.session_path)
    if not src.is_file():
        raise SystemExit(f"not a file: {args.session_path}")

    info = session_freshness(src)
    if info.get("backup_newer"):
        print(f"WARNING: sessionstore-backups/{info.get('newest_backup')} "
              "is NEWER than sessionstore.jsonlz4; state may be "
              "stale/incomplete. Firefox may still be running or may have "
              "crashed; treat output as a snapshot, not live ground truth.",
              file=sys.stderr)
    try:
        age = time.time() - info.get("mtime", time.time())
        print(f"session mtime age: {age:.0f}s", file=sys.stderr)
    except Exception:
        pass

    if args.no_copy:
        if _no_copy_refused(src, args.scratch):
            raise SystemExit(
                f"refusing --no-copy on {src}: not a scratch copy "
                "(no .copy. marker and not inside a scratch dir — "
                "copy the live session file first or drop --no-copy)")
        work = src
        made_copy = False
    else:
        scratch = Path(args.scratch) if args.scratch else Path(
            tempfile.gettempdir()) / "opencode"
        try:
            work = copy_session_safe(src, scratch)
        except ValueError as exc:
            raise SystemExit(str(exc))
        made_copy = True
        print(f"decoded safe copy: {work} (live file untouched)",
              file=sys.stderr)

    # Read + decode inside the guarded block: any failure (read error,
    # bad payload, missing lz4) still runs the finally that deletes the
    # scratch copy. Nothing may exit between copy creation and this try.
    # Trust boundary (S8707): `work` is either this script's own scratch
    # copy or a --no-copy path that passed the marker/containment guard.
    try:
        raw = work.read_bytes()  # NOSONAR pythonsecurity:S8707
        doc = decode_mozlz4(raw)
    except OSError as exc:
        raise SystemExit(f"cannot read {work}: {exc}")
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc))
    finally:
        if made_copy and not args.keep_copy:
            if not cleanup_session_copy(work):
                print(f"WARNING: could not delete scratch copy {work}; "
                      "delete it by hand (it may hold sensitive URLs).",
                      file=sys.stderr)

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
