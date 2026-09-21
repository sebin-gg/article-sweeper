# Changelog

All notable changes to this skill follow Keep a Changelog. Versions use
semver and match `metadata.version` in SKILL.md frontmatter.

## [1.3.7] - 2026-09-21

Added:

- Windows process-ownership proof (`verify_endpoint_process_windows`):
  the strongest identity for product-blind vendors, matching the
  listening process's executable image path (PowerShell
  `Get-NetTCPConnection` + `Get-Process`), injectable for tests and
  fail-closed on query errors. Shared CLI gate `confirm_endpoint()`
  now backs both `list_cdp_tabs.py --check-endpoint` and
  `cdp_close.py`: a vendor-blind product (verified live: Thorium
  reports plain `Chrome/...` with no Thorium token anywhere) may
  proceed ONLY when `--expect-cmd` also proves the listening process;
  both halves are required and generic-family names (chrome,
  chromium) are excluded from the exception. CLIs print a pointer to
  the `--expect-cmd` remedy when refusing without it.

Verified:

- Thorium (Thorium 140 binary, Chromium 138 base) close path verified
  end-to-end on Windows port 9222: vendor-blind identity + live
  process proof (`endpoint owner pid`), `CLOSED 1/1` exit 0 with
  `--expect` revalidation. Steady-state close latency 0.02s; the WSL
  async-close delay did not reproduce on Windows. Operational quirk
  documented: closing the browser's only page tab exits Thorium
  entirely (post-close recount then sees the endpoint gone — expected,
  not a failure). Brave remains verified in WSL only.

Security:

- AGENTS.md + `references/dev-mode.md`: image-name kills
  (`taskkill //IM`, `pkill <name>`) are forbidden alongside force-kill
  flags — verified live they signal the user's own session (default
  `User Data` profile) rather than only the verification instance.
  Resolve the exact PID via the port owner or `--user-data-dir`
  fragment first.

## [1.3.6] - 2026-09-21

Fixed:

- Endpoint identity: Vivaldi exposes no vendor token — verified live it
  reports `Browser: Chrome/8.2.4133.68` (its own version under a Chrome
  prefix) with a plain-Chrome User-Agent. The identity check now accepts
  a major-version mismatch between Browser field and UA as the Vivaldi
  signature (`_vivaldi_version_mismatch`, gated to `--browser vivaldi`,
  fails closed on missing versions). A consistent plain-Chrome endpoint
  can never produce the mismatch, so it still cannot pass as Vivaldi;
  branded names are still refused on such an endpoint.

Verified:

- Opera `OPR/136.0.0.0` (Chromium 152) close path verified end-to-end on
  Windows port 9228: real article tab, mandatory `--expect` revalidation,
  `--browser opera` identity, `CLOSED 1/1` exit 0, live recount confirms
  the tab gone, no unexpected closures.
- Vivaldi `Chrome/8.2.4133.68` close path verified end-to-end on Windows
  port 9227: same full pass (`CLOSED 1/1`, exit 0, recount clean).
  Steady-state close latency 0.01s — the previously reported "close
  hang" did not reproduce; it was root-caused to Vivaldi's first-run
  startup window where TCP accepts before `/json/version` answers
  (documented in `references/dev-mode.md` launch-handshake note).

## [1.3.5] - 2026-09-21

Fixed:

- Endpoint identity check: when `/json/version`'s `Browser` field does not
  match the expected browser, `check_endpoint_identity()` now consults the
  `User-Agent` as a conservative fallback — only vendor-distinctive tokens
  (`OPR/`, `Edg/`, `brave`, ...) may rescue the match; generic
  chrome/chromium tokens are excluded so a plain-Chrome endpoint can never
  pose as a branded browser (and vice versa). Motivated by a live finding:
  Opera 136 (Windows) reports `Browser: Chrome/152...` but keeps
  `OPR/136.0.0.0` in User-Agent. Tested: 3 new unit tests; live
  `--check-endpoint` pass on the real Opera instance.

Verified:

- Opera `OPR/136.0.0.0` (Chromium 152) verified live on Windows (own port
  9228, dedicated `--user-data-dir`, fresh install dir): CDP reachable,
  `--check-endpoint` passes with the UA fallback, tab enumeration works
  and correctly filters internal `chrome://` start-page targets. Full
  close-path still pending a live open-article run.

## [1.3.4] - 2026-09-21

Fixed:

- `cdp_close.py` polls the post-close list briefly (~3s) before failing:
  real-browser finding that Thorium answers 200 while still listing the
  target, with disappearance a beat later. Unreachable lists still fail
  immediately as UNVERIFIED.

Added:

- Documented Chrome 144+ user-consent existing-session path
  (`chrome://inspect/#remote-debugging` + per-connection approval
  dialog) as a host-capability-routed alternative to the dedicated
  profile, with explicit limits: MCP/Browser Use hosts only (not raw
  CDP scripts), Chrome ≥ 144 required, per-connection friction is by
  design. Sourced from Chrome for Developers + chrome-devtools-mcp
  docs; not live-tested from here (would touch a real user session).

## [1.3.3] - 2026-09-21

Verified:

- Chrome `Chrome/153.0.8010.53` and Edge `Edg/153.0.4234.48` verified
  end-to-end on Windows (headless, dedicated `--user-data-dir`, own
  loopback ports 9224/9226): list → close-one (`1/1`, exit 0) → live
  recount 0 pages, with `--browser` identity match (Edge exercised the
  `edg/` product alias). Recorded in `references/chromium.md`.
  Thorium, Brave, Vivaldi, Opera remain pending per-browser verification.

## [1.3.2] - 2026-09-21

Fixed:

- `find_pids_listening_on()` also reads `/proc/net/tcp6`: IPv6 (`::1`)
  listeners are invisible in `/proc/net/tcp`, so `--expect-cmd` used to
  fail closed on IPv6 endpoints. Regression test covers a tcp6-only
  owner (plus a live `::1` socket check).

## [1.3.1] - 2026-09-21

Added:

- Verified launch handshake: `sweep_lib.iter_candidate_ports()` (never
  repeats a port across retries) and `sweep_lib.wait_for_endpoint()`
  (polls `/json/version` until the launched browser reports back with
  the expected product). dev-mode.md documents the
  launch → poll → process-check → retry flow, closing the
  discover-to-bind TOCTOU gap by verification instead of reservation.

## [1.3.0] - 2026-09-21

Added:

- Process-level endpoint ownership: `sweep_lib.verify_endpoint_process()`
  maps a debug port to owner PID(s) via Linux `/proc` and matches the
  command line against an expected fragment. Opt-in via `--expect-cmd`
  on `cdp_close.py` and `list_cdp_tabs.py --check-endpoint`; fails
  closed where process lookup is unsupported.

Fixed:

- README no longer promises absolutes for non-articles ("never
  summarized/never closed") — states baseline + scope-rule exclusion.
- AGENTS.md test-gate wording no longer contradicts the PR-convention
  note.
- `find_free_port()` documents its TOCTOU limit and names the identity
  re-check as the actual enforcement; dev-mode.md repeats it.

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
