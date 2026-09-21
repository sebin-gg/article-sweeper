# Changelog

All notable changes to this skill follow Keep a Changelog. Versions use
semver and match `metadata.version` in SKILL.md frontmatter.

## [1.2.0] - 2026-09-21

Breaking:

- `cdp_close.py` requires `--browser` (endpoint identity is now always
  strict; there is no "some Chromium endpoint" mode).
- `list_cdp_tabs.py --check-endpoint` requires `--host`, `--port`, and
  `--browser` together.
- `cleanup_session_copy()` returns bool (True = nothing remains) instead
  of None; decode warns on False.

Fixed:

- Generic redirect-param unwrapping only runs on known wrapper domains
  or redirect-shaped paths — plain article URLs carrying `url=`/`to=`
  params are never rewritten.
- `recount_and_fix_header()` takes the same sidecar lock as
  `atomic_append()`, so a concurrent append cannot be lost by a rewrite.
- `--no-copy` accepts only the `.copy.` naming marker or containment in
  a known scratch dir (boundary-checked); substring matching removed.
- `copy_session_safe()` uses a unique destination per call (pid + random
  token); concurrent sweeps no longer share one scratch file.
- `TRACKING_PARAMS` drops `ref`/`referrer`/`spm` (not universally
  tracking); tracking detection is case-insensitive.
- SKILL.md no longer claims AMP/`?sk=`/author-subdomain collapsing —
  near-duplicates stay separate for agent judgment.
- `cleanup_session_copy()` uses directory-boundary containment and
  reports failure instead of swallowing it.
- `decode_mozlz4()` rejects non-object JSON; `session_freshness()`
  scopes backups to `*.jsonlz4` and names the newest snapshot.
- `endpoint_for()` accumulates into the caller's `taken` set.
- SKILL.md states fetched pages/search results are untrusted data
  (prompt-injection rule); AGENTS.md no longer claims PR enforcement
  that `main` lacks.

Added:

- Vendor product aliases for endpoint identity (Edge=`Edg/`,
  Opera=`OPR/`, Chromium≈Chrome).
- Mocked CDP integration suite (fake in-process CDP service): list
  check happy/wrong-browser, close happy/navigated/stays-listed/
  unreachable/unexpected-drop/wrong-browser/dead-port.

## [1.1.1] - 2026-09-21

Fixed:

- SKILL.md frontmatter is spec-conformant: `version` is a quoted string
  and `tags` a comma-separated string (the flow-style array failed
  `skills-ref validate`).
- `decode_firefox_session.py` now deletes the scratch copy even when the
  copy's own read fails (read moved inside the guarded try/finally).
- The recount claim is now literally true: new
  `sweep_lib.recount_and_fix_header()` recounts `## ` entries and rewrites
  the header count line atomically (SKILL.md and README name the helper).

Added:

- `skills-ref validate article-sweeper` (official spec validator) as a CI
  step and in the AGENTS.md verify block.
- Regression tests: read-failure copy cleanup, header rewrite/untouched
  cases; version lint tolerates quoted versions.

## [1.1.0] - 2026-09-20

Added:

- Deterministic core `scripts/sweep_lib.py`: URL unwrap/canonicalize with
  tracking-only param drops, dedupe, classifier baseline, CDP
  validation, close-candidate revalidation, atomic locked append with
  authoritative recount, per-browser endpoint discovery, log redaction.
- `tests/test_sweep_lib.py`: behavioral suite with mocked CDP and
  Firefox session fixtures; CI runs them on every push/PR.
- Execution-mode decision (§0.5): local scripts by default; Browser Use
  or Computer Use only on explicit request.
- `list_cdp_tabs.py --check-endpoint` (with `--host`/`--port`): validates
  `/json/version` identity before enumeration output, so a reused/wrong
  port cannot lead to summarizing the wrong browser's tabs.
- `sweep_lib.cdp_url()` / `cdp_host_for_url()`: centralized CDP URL
  construction with correct IPv6 bracketing (`http://[::1]:port/...`).

Changed:

- `list_cdp_tabs.py`: filters by CDP target type + internal schemes,
  rejects malformed entries, records endpoint/browser identity, optional
  `--redact`.
- `cdp_close.py`: strict port range (1–65535), loopback-only host,
  `--expect` pre-close revalidation (skips vanished/navigated tabs),
  post-close disappearance check instead of trusting HTTP 200.
- `decode_firefox_session.py`: copies the session file to scratch itself
  by default, warns on stale state (mtime age, newer backup), picks the
  tab's selected entry index-aware; removed the known-unreliable
  `tail`/`lz4cat` fallback (requires `pip install lz4`).
- `references/dev-mode.md`: Chrome 136+ needs a dedicated
  `--user-data-dir` (in-place default-profile debugging is obsolete),
  per-browser ports, safe PID identification, per-vendor restore checks,
  scratch cleanup policy.
- `references/chromium.md` / `firefox.md`: Firefox is read-only (manual
  close); derivatives are Chromium-compatible pending per-browser
  verification.
- README: dropped `zero-config` / `CDP ground truth` / `all detected
  browsers swept` overclaims; documented requirements, trust boundary,
  and Firefox manual close.
- CI (`lint.yml`): `actions/checkout@v7`, `actions/setup-python@v7`,
  plus a pytest job.
- No single end-to-end `sweep` executable by design: summarization needs
  agent judgment; deterministic primitives + SKILL.md orchestration
  documented in `references/chromium.md`.

Fixed:

- `cdp_close.py`: `--expect` is now required (no blind close-by-id
  path); unverifiable post-close states fail with non-zero exit;
  endpoint identity checked via `/json/version`
  (`sweep_lib.check_endpoint_identity()`) before any close; before/after
  set comparison runs in-script and fails on unexpected closures.
- `cdp_close.py` before/after diff uses the fresh live list taken just
  before closing as baseline (`--expect` stays the authorization
  snapshot only); tabs that closed naturally between `--expect` and
  revalidation no longer misreport as unexpected closures.
- `sweep_lib.atomic_append()`: mandatory locking on all platforms
  (`fcntl.flock` POSIX, `msvcrt.locking` Windows) — no silent no-op.
- `sweep_lib.copy_session_safe()`: stable-snapshot retry (two identical
  reads) instead of a single racy copy; failure path deletes any partial
  scratch copy before raising; new `cleanup_session_copy()` helper;
  `decode_firefox_session.py` deletes its scratch copy on all paths
  unless `--keep-copy`, and `--no-copy` is refused unless the path
  already looks like a scratch copy.
- `sweep_lib.redact_url()`: now best-effort across query, fragment,
  userinfo, and token-shaped path segments (JWT/long-token heuristic);
  docs no longer describe `--redact` as a general guarantee.
- Skill frontmatter `allowed-tools` uses the spec's space-separated form;
  §0.5 notes Browser/Computer Use branches need a host-exposed
  capability (no standardized tool name exists).
- Stale test-count docs replaced with "behavioral suite" wording.

Removed:

- Tracked `tests/__pycache__/` artifact; added `.gitignore`
  (`__pycache__/`, `*.pyc`, scratch captures).

## [1.0.1] - 2026-09-18

Changed:

- Renamed the skill from `browser-article-sweeper` to `article-sweeper`.
  Install with `npx skills add <owner>/article-sweeper@article-sweeper`.

## [1.0.0] - 2026-09-18

Added:

- Cross-browser article sweep for Thorium, Chromium, Chrome, Brave, Edge,
  Vivaldi, Opera, and Firefox.
- CDP tab enumeration and parallel close scripts.
- Firefox `sessionstore.jsonlz4` decode script.
- Append-only dated desktop summary file flow.
- Dev-mode restart procedure with session backup rule.
