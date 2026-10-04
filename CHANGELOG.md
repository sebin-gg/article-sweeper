# Changelog

All notable changes to this skill follow Keep a Changelog. Versions use
semver and match `metadata.version` in SKILL.md frontmatter.

## [Unreleased]

Added:

- Readability pass before any search fallback (`extract_readable`): structural
  junk (script/nav/footer/…) is stripped first, then a content-bearing
  `<article>`/`<main>` container is preferred when present. A JS-heavy page
  whose article renders into a container is recovered here instead of being
  written off as an empty shell and sent to search at extra cost.
- Challenge/gate detection (`looks_like_challenge`): captcha, bot-wall, and
  subscription-gate interstitials are labelled `challenge:<needle>` instead of
  being trusted as dense article text. Both the raw HTML and the extracted
  text are checked, because a bot wall's own markup is the more reliable
  signal.
- Page-title recovery (`page_title`): `og:title` then `<title>` from the
  fetched HTML beats the CDP history title (entities unescaped, whitespace
  normalised); placeholder titles ("Just a moment", "Human Verification",
  ALL-CAPS spam) are rejected so they can never be filed as an article title.
  Exposed as `page_title` on fetch results.

Changed:

- `MAX_TEXT_CHARS` drops from 200 000 to 16 000: the cap now bounds the text
  handed to the summariser (the largest avoidable prompt cost) instead of the
  classification input. Classification signals are computed on the FULL
  extracted text so density is not skewed by the cap; `truncated` records when
  the body was longer. Fetch results also carry `html_chars`, `challenge`, and
  `js_shell` (fewer than `MIN_WORDS` words of prose), stated plainly instead
  of handing the summariser a near-empty body.

Fixed:

- macOS process-ownership proof fails closed. `_darwin_listening_pids` parsed
  `lsof` stdout without inspecting its exit code or stderr, so a failed query
  (binary missing, not permitted) returned `[]` — indistinguishable from "no
  process owns this port". That silently skipped the ownership proof before any
  tab is closed, which is exactly the guard this helper exists to provide. Only
  a clean exit 0/1 with an empty stderr now counts as "nothing is listening";
  anything else raises, matching its sibling `_darwin_process_cmdline`.

Tests:

- `test_darwin_listening_pids_query_failure_does_not_look_empty` covers a
  non-zero exit with a stderr message, a hard failure exit, and a stderr
  message alongside otherwise-parseable output. Verified it fails against the
  previous behaviour.

## [1.12.0] - 2026-10-04

Dirty-form guard: never close a tab holding unsaved user input.

Added:

- `cdp_forms.py` -- a minimal, dependency-free RFC 6455 WebSocket client,
  because `Runtime.evaluate` has no HTTP equivalent and this skill must run on
  a bare Python install. Handshake verifies the `Sec-WebSocket-Accept` token,
  so a plain HTTP server answering `101` cannot fool it.
- `probe_dirty_form()` runs one `Runtime.evaluate` per close candidate,
  checking for non-empty `input`/`textarea`/`contenteditable` plus attached
  files. Checkboxes, radios and file inputs are excluded -- a checked box is
  not typed work -- but a single stray character counts, because leaving a tab
  open costs far less than losing typed work.
- `cdp_close.py` probes candidates in parallel before closing, and
  `TabRecord` now carries `webSocketDebuggerUrl` from `/json/list` so the
  probe reuses the snapshot already revalidated rather than re-fetching.
- `--no-form-guard` opts out.

Fails closed, deliberately:

- Anything short of a definitive answer -- no websocket URL, refused handshake,
  bad accept token, timeout, CDP error, unparseable result, probe crash --
  reports the tab as dirty and skips the close. A guard that silently passes
  when it cannot see is worse than no guard.
- The visible cost: on a browser that does not publish
  `webSocketDebuggerUrl`, this blocks every close until `--no-form-guard` is
  passed. That is the intended trade, and the flag is the documented escape
  hatch rather than a silent fallback.

Tests:

- 332 pass (was 313). 19 new: 15 unit tests against a real fake WebSocket
  server (`tests/fake_ws.py` implements the handshake, masked client frames
  and ping/pong, so the socket path is exercised rather than mocked), plus
  4 end-to-end through the CLI with real websockets behind the fake browser.
- Three new mutants in `mutation_guard.py`: failing open on transport error,
  ignoring the page's dirty flag, and skipping the guard entirely. All caught;
  11 mutants total, all caught.



Make the safety guarantees outlive the person who wrote them.

Added:

- `tests/test_regression_guards.py` -- a permanent contract lock. The critical
  route list is **duplicated into the test file on purpose**: deleting an entry
  from `NEVER_CLOSE_SEGMENTS` now fails CI instead of silently narrowing
  protection. Also asserts every listed segment actually blocks (no dead
  entries), that compound routes stay blocked, that real articles stay
  closable, and -- as a source-level backstop -- that `cdp_close.py` still
  calls the gate, so a single PR deleting both the gate and its tests fails.
- `tests/mutation_guard.py` -- repeatable mutation testing, now a required CI
  job. One-off mutation testing is only true on the day it is run; re-running
  the eight mutants that matter means a test which silently stops
  discriminating (an edited helper making its assertions vacuous) breaks the
  build instead of quietly voiding a guarantee.
  Each mutant is applied, the targeted tests must **fail**, and the file is
  always restored in a `finally`. It refuses to run on a dirty tree, since
  mutants are applied in place.

Found by the guard on its first run:

- A real coverage gap. Removing `NON_ARTICLE_SEGMENTS` matching flips `/mail`
  and friends to `default-candidate` with **every existing test still passing**.
  `cart` was masked because it is also in the harder `NEVER_CLOSE_SEGMENTS`,
  which runs first; the routes reachable *only* through the soft list had no
  direct test at all. Now covered by 17 parametrized cases.
- One mutant of mine was also wrong. Mutating `NON_ARTICLE_PATH_RE` turned out
  to be a semantic no-op, because that regex had already been anchored in the
  blocklist work -- only the segment set is a real lever. Fixed rather than
  left passing for the wrong reason.

Tests: 313 pass (was 278), plus 8 mutants verified caught.

## [1.11.0] - 2026-10-04

Hard never-close blocklist, and a real substring bug in the classifier.

Added:

- `sweep_lib.is_never_close()` and a `NEVER_CLOSE_SEGMENTS` set covering app
  routes: auth/identity (oauth, login, 2fa, mfa, otp, webauthn, passkey,
  recovery), money (checkout, cart, payment, billing, subscribe), unsaved
  state (edit, new, upload, compose, draft, apply, viewform), account/admin,
  and verification/confirm/activate prompts.
- Applied in **two independent places**. `classify_url()` refuses them as
  article candidates, and `cdp_close.py` re-checks at close time. The second
  gate cannot be talked out of it: a schema-valid summary entry does not
  unlock a tab on `/checkout`. Both gates must pass.
- Compound routes matched as multi-segment shapes too: `/two-factor`,
  `/challenge`, `/account/verify/...`, `/verify-email`, `/i/flow`, `/consent`.

Fixed:

- **A real substring bug in the pre-existing classifier.** `NON_ARTICLE_PATH_RE`
  was unanchored, so `/cart` matched `/blog/cartoon-history` and `/mail`
  matched `/blog/mailman-archive`. Articles on those topics could never be
  classified and were silently left open forever. Now segment-matched.
- `is_never_close()` raised `AttributeError` on non-string input from a
  malformed dump. A safety gate must never raise on junk.

Notes:

- Matching is on whole path **segments**, never substrings. `/blog/cartoon-
  history`, `/blog/how-to-edit-video`, `/blog/mailman-archive` and
  `/posts/2024/10/verify-your-backup` all stay closable; a substring blocklist
  would silently neuter the entire tool.
- The blocklist reason never echoes the query string, so a URL like
  `/checkout?card=4111...` cannot leak a card number into stderr.

Tests:

- 41 new tests, 278 pass, mutation-tested. Substring matching fails 8,
  disabling the blocklist fails 21, and removing the close-time gate fails 7 --
  the last being the one that matters, since it is the only proof that the
  gate is genuinely independent of the classifier.
- Two of my own test bugs caught by mutation and by the last-page guard: a
  helper that built the fake CDP server without entering it, making every
  "was not closed" assertion pass vacuously; and a test that requested every
  page tab, which the last-page guard correctly refused.

## [1.10.1] - 2026-10-04

Two title-parsing bugs found while testing #14, plus the coverage that finds
them.

Fixed:

- **Bot-wall placeholders leaked through as titles.** `_is_placeholder_title()`
  matched exactly, so the title a challenge page actually ships --
  `"Just a moment..."` -- was accepted and filed as an article title. Matching
  now happens on the stripped core, so a trailing ellipsis no longer defeats
  it. Verified: `page_title("<title>Just a moment...</title>") == ""`.
- **Typographic entities were not decoded.** `og:title` values routinely carry
  `&mdash;`, `&ndash;`, `&hellip;` and curly quotes, which reached the summary
  verbatim. Now decoded to the actual characters.

Tests:

- 39 tests added over the #14 code, including the fetch-path contract
  (`truncated`, `challenge`, `js_shell`, `page_title`) that #14 shipped
  without.
- `test_readability_excludes_body_content_outside_main` is the one that
  matters most: dropping the `<main>` preference entirely passes every other
  readability assertion, because the junk regex already strips nav/footer and
  the container choice only shows up against ordinary body content. Found by
  mutation testing, not by reading the code.

## [1.9.0] - 2026-10-03

The summary file becomes the close gate, so the run audits itself.

Added:

- **`--summary` is now required by `cdp_close.py`.** A tab closes only if its
  canonical URL has an entry in the summary file *and* that entry is
  schema-valid: `## ` title, a `Link:` parsing as an http(s) URL, non-empty
  `Summary:`, non-empty `Takeaway:`, and a `---` terminator.
- New `sweep_lib.validate_summary_entry()` and `parse_summary_file()`. Only
  valid entries enter the index, so membership is itself proof of
  completeness.
- Matching is on the canonical URL, so a `Link:` written in a cleaner form than
  the tab's own URL (`HTTPS://EX.COM:443/a#top`) still authorizes it.
- An empty or missing summary refuses the whole run; invalid entries are
  printed as `SUMMARY-PROBLEM` rather than silently dropped.

Why:

- Previously nothing tied the close set to the summary. "Closed but never
  summarized" was prevented only by the agent remembering to check, which is
  exactly the kind of discipline a destructive action must not depend on.
- With the gate, every close is backed by an entry you can read, and the
  summary stops being a report and becomes the authorization record. One
  invariant, and the whole run is auditable after the fact.
- Schema validation matters because a truncated or half-written entry is
  precisely what a crash mid-stream leaves behind; `SummaryStream` already
  makes such files visible, and this makes them inert.

Tests:

- 13 new tests, 198 pass, mutation-tested. Removing schema validation fails 9;
  removing the gate itself fails 9. Seven parametrized cases pin the schema
  rules, including a `javascript:` Link, which must never authorize a close.
- One test asserts the documented `skipped-paywalled` entry shape still
  validates, so tightening the schema cannot silently outlaw a format the
  skill tells the agent to produce.
- A wrong assumption of mine surfaced here: I assumed `?utm=1` would collapse
  to the bare URL in a duplicate check. It does not, by design -- query params
  are significant and only known tracking params are dropped. The code was
  right; the test now uses a genuine canonicalization.

## [1.8.0] - 2026-10-03

Close tabs only if they are still the tabs you approved.

Fixed:

- **The enumerate/close race was only half-closed.** `verify_close_candidates()`
  re-listed once for the whole batch, then every close ran in a 20-wide pool.
  That check therefore covered a single instant: on a 50-tab sweep the last
  close fires many seconds after the snapshot it was approved against, and a
  tab that navigated in between was closed on a stale decision.
- `close_one()` now re-lists and re-compares the canonical URL immediately
  before issuing the `PUT /json/close/<id>`, per tab, carrying the URL it was
  approved for. Three outcomes skip the close: target gone, canonical URL
  changed, or list unreachable. Unverifiable means "do not close", never
  "close and hope".
- Skips are reported and exit non-zero, so a partially-applied close set is
  visible rather than silently short. Cost is one local `/json/list` read per
  close, which is what buys "approved now" instead of "approved a moment ago".

Tests:

- 4 new tests, 185 pass. Two of the first drafts passed under the *pre-fix*
  code, because they were satisfied by the old batch check rather than the
  new per-close one -- passing for the wrong reason. Both now fire after the
  batch approval (list failure and tab removal begin on the second list read),
  and under the pre-fix behaviour all three skip-path tests fail.
- The navigated-tab race is driven through a real fake CDP server that
  rewrites tab A's URL on the second `/json/list` read, so the navigation
  genuinely lands in the window between batch approval and close.

## [1.7.0] - 2026-10-03

Resilience under rate limits, and a streaming summary writer.

Retry / backoff (`fetch_articles.py`):

- `fetch_one()` now retries transient failures — `408/425/429/500/502/503/504`
  and transport errors — with exponential backoff plus **jitter**, up to
  `--max-attempts` (default 3), honouring a `Retry-After` header when sent.
- Refusals (`401/402/403/451`) fail on the first attempt. Retrying them is
  pointless and only annoys the publisher.
- Every outcome, success or failure, now reports `attempts`. An exhausted `429`
  still reports `throttled`; an exhausted `503` reports `error`, so the summary
  line can distinguish "rate limited" from "fetch failed".
- Jitter is the point, not decoration: without it, N tabs against one host
  retry in lockstep and re-create the stampede that caused the 429.

Streaming summary writer (`sweep_lib.SummaryStream`):

- Replaces rigid summarize batches. Each entry is durable the moment it is
  summarized, so losing the process later cannot lose earlier work.
- **Partial writes**: every entry goes through `atomic_append()` — one locked,
  fsynced write of complete lines — so a crash leaves whole entries only. An
  entry can never be half-written because it is fully rendered before the
  write starts.
- **Mid-stream failures**: the header declares the run's `expected` count
  *before* any entry lands, and is recounted against the real entry count as
  each entry lands. A process killed mid-stream therefore leaves a file that
  is already self-evidently short, without needing `finalize()`.
- `finalize()` reconciles the count to the truth and appends a machine-readable
  `<!-- sweep-stream: emitted=N expected=M complete|partial -->` trailer.
  `partial` means entries are missing: re-run those URLs rather than close tabs
  against a short summary.

Pinned the interaction between the per-domain cap and retry backoff, which had
been an accident of nesting rather than a guarantee. `fetch_one()` sleeps
inside the per-domain gate, so a URL parked in backoff keeps its host's
semaphore held: a `429` therefore slows every URL on that host, not just the
one that hit it. That is the intended reading -- a `429` is the host asking for
slower traffic, so the penalty belongs to the host that caused it rather than
to the rest of the sweep, and other hosts keep running untouched. It is also
what makes the cap's serialization worth its cost: one host's throttle becomes
a local slowdown instead of a sweep-wide one.

The first version of those tests was vacuous. They patched `time.sleep` to a
no-op, which erased the very window they claimed to observe, so a mutation
letting the backoff escape the gate still passed. Rewritten to use a real
blocking sleep, after which that mutation fails both tests. A concurrency test
that mocks out the delay it depends on is not a concurrency test.

Tests:

- 24 new tests, 181 pass. Mutation-tested rather than trusted: removing the
  retry classification fails 4, removing jitter fails 1, dropping the running
  recount fails 1, stale header intent fails 1, letting backoff escape the host
  gate fails 2.
- The stale-header case is worth noting: mutation testing found a gap where
  *nothing* pinned the initial header, so a crash before the first emit would
  have looked like an empty sweep rather than a lost one. That test exists now.

## [1.6.0] - 2026-10-03

Fetch once, use the body for both classification and summarization.

Added:

- **Content refinement** (`sweep_lib.refine_by_content`, `looks_like_article`,
  `content_signals`, `link_density`, `html_to_text`). The URL gate already
  excludes webmail, repos and dashboards, so a fetched body is one that was
  intended to be read — which makes it free evidence for the decision still
  open. Density and readability beat guessing from a title, at no extra request.
  Thresholds: 220 words, 3 paragraphs, long-word ratio >= 0.12, link density
  <= 0.45.
- `fetch_articles.py --with-text` returns the extracted text and its density
  signals on the same request. The sweep now feeds **both** `article` and
  `unsure` URLs to the fetcher, then refines before anything closes.

Safety:

- Refinement is **one-directional**. It may only promote `unsure` -> `article`.
  It never touches `leave-open` and never demotes. A web inbox is dense with
  text, so any rule that could promote `leave-open` would close the busiest
  dashboard in a sweep instead of the article. Verified: a `leave-open`
  webmail URL stays `leave-open` when handed article-shaped prose.
- This also removes the silent-failure mode behind `unsure`: before, a weak
  signal meant the article simply never got swept and nothing said so. Now the
  body it already paid for can resolve it, and only genuinely ambiguous pages
  reach a model or stay open.

Fixed:

- `html_to_text` collapsed the whole document to a single line because the
  block-level regex was defined but never applied, which silently killed every
  paragraph-based density signal. Block closers now become newlines before tags
  are stripped.

Tests:

- 21 new tests. Reverting the paragraph fix fails 3 of them, and removing the
  per-direction guard breaks the webmail case, so the suite is not vacuous.

## [1.5.2] - 2026-10-03

Closes two gaps in the 1.5.1 dev-mode hardening.

Fixed:

- The 1.5.1 address binding was **incomplete**: it covered the
  dedicated-profile launch example and missed the three restart-launch
  commands in `dev-mode.md` (Linux, macOS, Windows Brave). All launch
  examples now pass `--remote-debugging-address=127.0.0.1`.
- `dev-mode.md` required taking a backup before any kill but never required
  **verifying** it. An interrupted or truncated copy leaves a directory that
  looks like a backup, and restarting against it trades in-progress tabs for a
  corrupt session. Added explicit checks: `Sessions/` non-empty,
  `Preferences` parses as JSON (or `prefs.js` non-empty), file count and byte
  total plausible versus the source, and no file newer than the backup's own
  start. Any failure means do not restart.

Tests:

- 3 new tests: every browser launch example binds the debug port to loopback
  (catches the exact omission above), the port is documented as an
  unauthenticated control channel, and the backup must be verified.

## [1.5.1] - 2026-10-03

Hardening pass responding to the drawbacks raised against 1.5.0.

Fixed:

- **Per-domain fetch concurrency (feedback #4).** `fetch_articles.py` ran a
  single global pool of 20, so every concurrent fetch hit the *same* host:
  eight Medium tabs meant eight simultaneous requests to Medium, which is how a
  sweep collects a 429 and a temporary ban. Concurrency is now keyed per host
  via `--per-domain` (default 2), so breadth still scales across domains while
  any one host stays polite. Verified: the same workload peaks at 8 concurrent
  per host ungated and exactly 2 with the cap.
- **`unsure` is a real decision, not a guess (feedback #2 and #3).** The typed
  gate mapped `default-candidate` to `article`, so a newsletter on `/lp/` or
  any app-like path became an article — and `article` feeds the one
  irreversible step in a sweep. `default-candidate` now returns `unsure`, which
  `TabDecision.is_closable` reports as `False`. Only `article-path-hint` and
  `user-named` authorize a close on their own. Verified against the previous
  behaviour: `https://newsletter.io/lp/q3-update` classified as `article`
  before, `unsure` now.

Changed:

- SKILL.md documents that `unsure` must be judged or left open, never promoted
  in bulk.
- Dev-mode examples bind `--remote-debugging-address=127.0.0.1` explicitly and
  carry a warning that the debugging port is an unauthenticated control channel
  readable by any local process (feedback #1).
- Title-only paywall entries must be filed as `NOT SUMMARIZED` with the title
  quoted verbatim, never paraphrased into a claim, so they cannot be mistaken
  for real summaries (feedback #5). The paywall denylist is documented as a
  floor: a live `blocked` result outranks it.
- Documented that a small `bytes` with `status: ok` usually means an empty
  JS shell rather than an empty article.

Tests:

- 17 new tests covering `unsure` semantics and close eligibility,
  `STRONG_ARTICLE_REASONS`, host normalisation, the per-domain cap holding
  under concurrency, and that the ungated version demonstrably violates it.

## [1.5.0] - 2026-10-03

Addresses customer feedback 3-6.

Added:

- **Typed classifier gate** (`sweep_lib.classify_tabs_typed`). One pass, no
  prose, no output tokens: every tab returns `article`, `leave-open`, or
  `duplicate-of:<url>`. Only surviving `article` entries reach the expensive
  summarizer. Duplicates point at the keeper so every id in the group still
  closes together; near-duplicates the canonicalizer does not merge (AMP,
  share tokens, author subdomains) stay separate decisions for judgment.
- **`scripts/fetch_articles.py`** — concurrent body fetch, default 20 at a time
  (the `xargs -P 20` shape), streaming one typed JSON outcome per URL so
  blocked hosts route without re-inspecting anything. Fetches start as soon as
  the typed gate emits URLs and overlap with the rest of classification instead
  of starting after it.
- **Script inventory** at the top of SKILL.md. Every helper is already shipped;
  the feedback reported writing `list_cdp_tabs.py`, `cdp_close.py` and a
  Firefox session decoder from scratch, which cost most of the setup time. The
  scripts existed but were only referenced inline mid-workflow.
- **Paywall policy** (`sweep_lib.plan_fetch`, `is_paywalled`,
  `is_blocked_status`). Paywalled domains skip the search fallback **by
  default** and get an honest title+domain entry instead;
  `--allow-search-fallback` opts back in. A 401/402/403/451 sets
  `needs_search` so those route in parallel rather than queueing behind the rest.

Fixed:

- `fetch_one` no longer aborts a whole run on one unparseable URL:
  `urllib.request.Request()` raises `ValueError` at construction, which sat
  outside the try block, so a single junk input line lost every other result.

Tests:

- 34 new tests: typed decisions, duplicate collapsing, significant-query
  preservation, user-named override, paywall detection, default-vs-opt-in fetch
  planning, blocked-status handling, malformed URLs, and the fetcher CLI.
- `lint` job now compiles `fetch_articles.py` too.

## [1.4.0] - 2026-10-02

Added:

- macOS process-ownership proof: `find_pids_listening_on` falls back to
  `lsof -iTCP:<port> -sTCP:LISTEN -Fp` and `read_process_cmdline` to
  `ps -o args=` when the real `/proc/net` tables are absent (macOS).
  Injected `proc_root` fixtures keep the Linux path; both helpers fail
  closed on query failure. Added to CI as a `test-macos`
  (macos-latest) job running the same hash-locked behavioral suite.

## [1.3.10] - 2026-09-21

Fixed:

- `cdp_close.py --allow-last-tab`: when every close applies and the
  endpoint then disappears (verified live behavior of browsers that
  exit on their last page close), the run now reports `allowed
  last-tab shutdown` and exits 0 instead of failing as UNVERIFIED —
  matching the documented contract. Any other unreachable-endpoint
  failure still fails closed.

Added:

- Runtime ambiguity notice: when `--browser chrome|chromium` passes on
  a `Chrome/...` product string without `--expect-cmd`, cdp_close
  warns that vendor-blind forks share that string and recommends the
  process-ownership proof. CDP cannot cryptographically distinguish
  real Chrome from such forks; the notice states the limit instead of
  overclaiming identity.

Changed:

- SKILL.md verification status synced with reality: all seven
  `DEFAULT_PORTS` Chromium-family browsers verified end-to-end on
  Windows (previously claimed "Chrome and Edge verified, rest
  pending").
- Dependabot: dropped the pip ecosystem (deps are hash-locked in
  `requirements-ci.txt`; bump via pip-compile) — fewer weekly PRs,
  no duplicate automation.
- CodeQL security scanning (`.github/workflows/codeql.yml`, python +
  actions, SHA-pinned actions, weekly schedule + PR/push triggers).
  The default-setup API is not available on this plan, so the scanner
  is defined as a versioned workflow instead.
- `sweep_lib.endpoint_gone_confirmed()` (new in this version) builds
  its probe URL via `cdp_url()` instead of a second `http://` format
  string — one URL builder, loopback-only by construction — and its
  exception mapping moved into the pure `_probe_failure_state()`
  helper (behavior unchanged; clears the SonarCloud S5332/S3776
  findings on the new code).

## [1.3.9] - 2026-09-21

Fixed:

- SonarCloud security gate: all six open vulnerability findings
  resolved for real rather than annotated — the workflow now installs
  CI deps (including `skills-ref`, now consumed from PyPI instead of
  an unpinned git URL) from one fully hash-locked
  `requirements-ci.txt` (`--only-binary` + `--require-hashes`, every
  artifact of every pin hashed via pip-tools); `lint.yml` YAML
  NOSONAR comments (which Sonar never honored) removed; the one
  un-annotated S8707 sink in `copy_session_safe` (`scratch.mkdir`)
  annotated with the existing trust-boundary rationale.

Verified:

- Chromium `Chrome/153.0.8010.53` close path verified end-to-end on
  Windows port 9223 (self-identifies via the generic `chrome` alias):
  `CLOSED 1/1` exit 0 with `--expect` revalidation, bystander article
  left open. All seven Chromium-family browsers in `DEFAULT_PORTS`
  are now verified on Windows.

## [1.3.8] - 2026-09-21

Added:

- Last-page guard: `sweep_lib.last_page_guard()` + `cdp_close.py
  --allow-last-tab`. Verified live (Thorium, Windows): closing a
  browser's only page tab exits the whole browser, losing the endpoint
  and any tabs meant to stay open. A close set that would leave zero
  page tabs is now refused by default with an actionable remedy;
  `--allow-last-tab` opts in explicitly. Singleton browsers are the
  case detectable with certainty over CDP; multi-window last-window
  variants are out of scope and documented as such.

Verified:

- Brave `Chrome/153.0.8010.53` (Chromium 153 base) close path verified
  end-to-end on Windows port 9225: vendor-blind product (plain
  `Chrome/...`, no Brave token in Browser or UA — unlike the WSL-era
  builds) + live Windows process proof (`--expect-cmd brave`),
  `CLOSED 1/1` exit 0 with `--expect` revalidation; bystander article
  stayed open. All six Chromium-family browsers in `DEFAULT_PORTS` are
  now verified end-to-end on Windows; Brave's earlier verification was
  WSL-only.
- Full suite green on Windows (76 passed, 2 skipped) and WSL
  (78 passed, 0 skipped — the /proc-only process tests run on Linux).

Documentation:

- SKILL.md §3: leave-open selection must respect `last_page_guard()` —
  hold one article-shaped tab back (or ask the user) when the close set
  would empty a browser; `--allow-last-tab` is for explicit
  user-approved shutdowns, not normal sweeps.
- README: per-browser verification matrix for v1.3.8 (versions, ports,
  identity mechanism, results) with the known vendor quirks.

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
