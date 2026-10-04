#!/usr/bin/env python3
"""Repeatable mutation testing: prove the safety tests can still fail.

One-off mutation testing tells you a suite is good today. This re-runs the
mutants that matter on every CI build, so a test cannot quietly stop
discriminating later -- for example when someone edits a helper and the
assertions it powers become vacuous.

For each mutant: apply it, run the targeted tests, and REQUIRE that they
fail. A surviving mutant means the tests were not actually testing that
behaviour.

Run:  python3 tests/mutation_guard.py [--keep-going]
Exit: 0 = every mutant was caught, 1 = at least one survived.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "article-sweeper" / "scripts"

SWEEP = SCRIPTS / "sweep_lib.py"
CLOSE = SCRIPTS / "cdp_close.py"
FORMS = SCRIPTS / "cdp_forms.py"
RULES = SCRIPTS.parent / "rules.json"

# (name, file, old, new, pytest -k selector, [extra paths])
MUTANTS = [
    (
        "blocklist-never-fires",
        SWEEP,
        "        if seg in NEVER_CLOSE_SEGMENTS:",
        "        if False:",
        "never_close or blocklist",
        None,
    ),
    (
        "blocklist-substring-matching",
        SWEEP,
        "        if seg in NEVER_CLOSE_SEGMENTS:",
        "        if any(k in seg for k in NEVER_CLOSE_SEGMENTS):",
        "never_close or blocklist",
        None,
    ),
    (
        "close-time-gate-removed",
        CLOSE,
        "    safe = [t for t in safe if not is_never_close(t.url)[0]]",
        "    pass",
        "blocklist",
        None,
    ),
    (
        "classifier-stops-honouring-blocklist",
        SWEEP,
        "    blocked, why = is_never_close(u)\n    if blocked:",
        "    blocked, why = False, ''\n    if blocked:",
        "never_close or blocklist or classifier",
        None,
    ),
    (
        # Removing segment matching from the classifier entirely. The older
        # regex is already anchored, so mutating the *segment set* is the
        # meaningful lever -- mutating the regex was a semantic no-op.
        "classifier-segment-matching-removed",
        SWEEP,
        "            s in NON_ARTICLE_SEGMENTS for s in _url_path_segments(u)):",
        "            False for s in _url_path_segments(u)):",
        "segments or classifier or never_close",
        None,
    ),
    (
        "summary-gate-becomes-a-no-op",
        CLOSE,
        "    safe = [t for t in safe if t.canonical in sindex]",
        "    pass",
        "summary or gate",
        None,
    ),
    (
        "placeholder-titles-accepted-again",
        SWEEP,
        "    if _PLACEHOLDER_TITLE_RE.match(text.strip().rstrip(\". \")):",
        "    if False:",
        "page_title or placeholder",
        None,
    ),
    (
        # The fail-closed property is the whole point of the dirty-form guard.
        "form-guard-fails-open-on-error",
        FORMS,
        '        return True, f"probe-failed:{type(exc).__name__}:{str(exc)[:60]}"',
        '        return False, "probe-failed"',
        "cdp_forms",
        None,
    ),
    (
        "form-guard-ignores-page-dirty-flag",
        FORMS,
        '        if value.get("dirty"):',
        "        if False:",
        "cdp_forms",
        None,
    ),
    (
        "form-guard-accepts-half-parsed-dom",
        FORMS,
        '        if ready == "loading":',
        "        if False:",
        "cdp_forms",
        None,
    ),
    (
        "form-guard-skipped-entirely",
        CLOSE,
        "    if not args.no_form_guard:",
        "    if False:",
        "form_guard",
        None,
    ),
    (
        # rules.json is supposed to be the source of truth. If a rule can be
        # edited in Python and still take effect, the file is decoration.
        "rules-json-ignored-segment-set",
        SWEEP,
        'NEVER_CLOSE_SEGMENTS = frozenset(RULES["never_close"]["segments"])',
        'NEVER_CLOSE_SEGMENTS = frozenset(RULES["never_close"]["segments"]) - {"checkout"}',
        "rules or never_close",
        None,
    ),
    (
        "rules-json-ignored-non-article-set",
        SWEEP,
        'NON_ARTICLE_SEGMENTS = frozenset(RULES["non_article"]["segments"])',
        'NON_ARTICLE_SEGMENTS = frozenset(RULES["non_article"]["segments"]) - {"cart"}',
        "rules or classifier",
        None,
    ),
    (
        "rules-loader-swallows-missing-file",
        SWEEP,
        '    p = Path(path) if path else RULES_PATH',
        '    p = Path(path) if path and Path(path).exists() else Path("/dev/null")',
        "rules",
        None,
    ),
    (
        "challenge-detection-disabled",
        SWEEP,
        "    for needle in CHALLENGE_PATTERNS:",
        "    for needle in ():",
        "challenge",
        None,
    ),
]


def _run(cmd: list[str]) -> int:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True,
                          text=True).returncode


def _out(cmd: list[str]) -> str:
    """stdout of a command. _run() returns only a return code."""
    return subprocess.run(cmd, cwd=ROOT, capture_output=True,
                          text=True).stdout


def _tree_is_clean() -> bool:
    """Only tracked modifications matter. An untracked scratch file is not
    mutated source and must not block the guard."""
    return not _out(["git", "status", "--porcelain",
                     "--untracked-files=no"]).strip()


def check_mutant(name, path, old, new, selector, extra) -> tuple[bool, str]:
    original = path.read_text(encoding="utf-8")
    if old not in original:
        return False, f"ANCHOR NOT FOUND in {path.name}: {old[:60]!r}"
    backup = tempfile.NamedTemporaryFile(
        "w", dir=str(path.parent), delete=False, encoding="utf-8")
    try:
        backup.write(original)
        backup.close()
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        cmd = [sys.executable, "-m", "pytest", "tests/", "-q", "-x", "-k", selector]
        rc = _run(cmd)
    finally:
        # Always restore: leaving a mutant behind is worse than no check.
        shutil.move(backup.name, str(path))

    if path.read_text(encoding="utf-8") != original:
        # A killed run (SIGKILL, or a harness timeout) skips `finally` and can
        # leave production source mutated. That actually happened here: a
        # background run was cut off mid-mutant and `cdp_close.py` was left
        # with the form guard disabled. Restoration is verified, not assumed.
        print(f"FATAL: {path} was not restored; it is still mutated.",
              file=sys.stderr)
        raise SystemExit(3)
    if rc == 0:
        return False, "SURVIVED - the selected tests still passed"
    return True, "caught"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-going", action="store_true",
                    help="report every result instead of stopping at the first")
    args = ap.parse_args()

    dirty = _out(["git", "status", "--porcelain",
                    "--untracked-files=no"]).strip()
    if dirty:
        print("refusing to run: mutants are applied in place, and the tree is "
              "already modified. A previous run may have been interrupted "
              "mid-mutant. Stash or commit first.\n" + dirty, file=sys.stderr)
        return 2

    print(f"mutation guard: {len(MUTANTS)} mutants\n")
    survivors = []
    for name, path, old, new, selector, extra in MUTANTS:
        caught, detail = check_mutant(*MUTANTS[[m[0] for m in MUTANTS].index(name)])
        mark = "ok  " if caught else "FAIL"
        print(f"  [{mark}] {name}: {detail}")
        if not caught:
            survivors.append(name)
            if not args.keep_going:
                break

    leftover = _out(["git", "status", "--porcelain",
                     "--untracked-files=no"]).strip()
    if leftover:
        print("\nFATAL: source left mutated after the run:\n" + leftover,
              file=sys.stderr)
        return 3
    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {survivors}")
        print("These tests no longer discriminate that behaviour.")
        return 1
    print("\nall mutants caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
