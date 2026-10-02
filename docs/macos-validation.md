# macOS validation results

Programmatic macOS runs via GitHub Actions `macos-latest` (no local Mac
required). Each PR push re-runs the full behavioral suite on Apple
hardware; the job also writes a step-summary block (runner OS, Python,
result, run URL).

## Latest run — 2026-10-02

- **Result:** success — `84 passed in 47.79s`
- **Runner:** `macos-latest` (GitHub-hosted), Python 3.14
- **Run:** https://github.com/sebin-gg/article-sweeper/actions/runs/37022958321
- **Scope:** full hash-locked behavioral suite
  (`python -m pytest tests/ -q`), same `requirements-ci.txt` as Linux

## What the first macOS run found (and the fix)

The initial `test-macos` run failed **2 of 81** tests — the
process-ownership proof was Linux-only:

- `test_find_pids_sees_real_ipv6_listener` — `find_pids_listening_on`
  parsed `/proc/net/tcp{,6}`, which does not exist on macOS, so every
  lookup returned `[]`.
- `test_cdp_close_expect_cmd_mismatch_refuses` — same root cause: the
  fail-closed path reported "no local process found listening" before
  the expected refusal message.

Fix (v1.4.0): on macOS (real `/proc/net` absent), the lookup falls back
to `lsof -iTCP:<port> -sTCP:LISTEN -Fp` and command lines to
`ps -o args=`; both fail closed on query failure. Injected `proc_root`
fixtures keep the Linux path covered. Re-run: **84 passed** on macOS.
