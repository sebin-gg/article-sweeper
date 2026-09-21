# AGENTS.md — article-sweeper

Behavioral rules for AI agents working in this repository. Read this before touching anything.

## 🛑 Critical Rules

- **Conventional Commits only:** `feat|fix|perf|docs|test|refactor|ci|chore|revert(scope):` (see `commitlint.config.cjs` if present). Don't use `--no-verify` unless it's genuinely broken.
- **Never commit sensitive data:** scratch captures (`cdp*.json`, Firefox `*.copy.jsonlz4` session copies, dev logs) may contain tokens, document IDs, and invite codes in URLs. Never commit them. Prefer `--redact` and delete scratch at end of each run.
- **Never push directly to `main` without tests:** the CI job (`python -m pytest tests/`) must pass before any merge. Run tests locally first (`python -m pytest tests/ -q`).
- **Don't modify session files in place:** always copy the Firefox session file to scratch first (`copy_session_safe`). Never read a live session file directly.
- **Never force-kill a browser:** `pkill -9` and bare `pkill <name>` can hit unrelated browser instances. Always identify the exact binary path before signaling, and use SIGTERM.
- **Remote debugging is loopback-only by design:** connecting CDP to a logged-in session exposes accounts, cookies, and page content. Never bind to `0.0.0.0`.

## Architecture contract (non-negotiable)

- **Deterministic core over prose.** The most important guarantees (URL normalization, deduplication, classification, close revalidation, atomic append) live in `scripts/sweep_lib.py`. Do not reimplement these rules in SKILL.md prose — import and use them.
- **Close verification is mandatory.** Every close candidate must be revalidated against a fresh `/json/list` before closing (same canonical URL + still exists). `cdp_close.py --expect` is required — there is no blind close-by-id path. Unverifiable closes fail the run. Never close by ID alone.
- **Tab IDs are ephemeral.** Between enumeration and close, a tab may navigate/close/reopen. The before/after set comparison (`diff_tab_sets`) must run after every close pass and report unexpected closures.
- **Firefox enumeration is read-only.** There is no automated Firefox close mechanism in this skill. Close Firefox tabs by hand from the printed list and report summarized vs closed counts separately.
- **Per-browser endpoints.** Each Chromium-family browser gets its own loopback port (see `sweep_lib.DEFAULT_PORTS`). A single global 9222 cannot serve multiple browsers. Validate endpoint identity via `/json/version` (`sweep_lib.check_endpoint_identity()`) before operating — a reused port may host a different CDP service. Chrome 136+ additionally requires a dedicated `--user-data-dir`.
- **Staleness warnings for Firefox.** The session file is a continuously-persisted snapshot, not instantaneous live ground truth. Always report mtime age and whether a backup is newer.

## Verify (run before you commit)

```powershell
python -m pytest tests/ -q          # behavioral suite (mocked CDP + Firefox fixtures)
python -m compileall -q article-sweeper/scripts  # all scripts compile cleanly
skills-ref validate article-sweeper  # official spec validator (pip install from agentskills/agentskills#skills-ref)
python -c "import re; from pathlib import Path; root=Path('article-sweeper'); text=(root/'SKILL.md').read_text(); fm=text.split('---')[1]; name=re.search(r'^name:\s*(\S+)',fm,re.M).group(1); assert name==root.name; v=re.search(r'^\s*version:\s*[\"'']?([^\s\"'']+)',fm,re.M).group(1); assert '['+v+']' in Path('CHANGELOG.md').read_text()"  # skill lint
```

The CI (`lint.yml`) runs skill-spec validation + lint + test on every push/PR.

## Test structure

- `tests/test_sweep_lib.py` — behavioral tests with mocked CDP and Firefox session fixtures. Covers: port/host validation, URL unwrap/canonicalize, dedupe, classifier, CDP parse/validation, close candidate revalidation, diff sets, atomic append, recount-and-fix-header, Firefox decode + copy + read-failure cleanup, session freshness.
- Add a test for every new rule in `sweep_lib.py`. No new behavior without a test.

## Push / PR (how changes land)

- **Never push straight to `main` without CI.** The remote enforces that changes come through a PR with passing tests. Before pushing anything, `git fetch origin` and rebase/merge the latest `main`.
- **Flow:** `git fetch origin` → branch off up-to-date `main` → commit (`conventional` only) → push branch → open a PR → wait for every gate to finish → merge when all green.
- **Code quality:** keep functions simple. Prefer pure functions in `sweep_lib.py` over agent-side logic.

## Docs / conventions

- Update `README.md`, `SKILL.md`, and `CHANGELOG.md` when commands, behavior, or the security model change.
- Any meaningful architecture decision → update `references/dev-mode.md` or the relevant reference file.
- Never commit generated artifacts or scratch captures.
- Drop overclaims from docs (e.g., "zero-config", "CDP ground truth", "all browsers swept"). State what is actually implemented and verified.

## Package manager preference

- Use `pnpm` for JavaScript/TypeScript package management on this system where applicable. Prefer `pnpm install`, `pnpm add`, `pnpm run <script>`.
- Run package/tool commands directly (`pip`, `pnpm`, `npm`, `python`, `cargo`, `uv`). For networked installs or broad dependency changes, confirm with the user first.
- Use modern CLI tools where available: `zoxide` instead of `cd`, `rg` instead of `grep`, `fd` instead of `find`, `bat` instead of `cat`, `eza` instead of `ls`.
- Container engine: **use `podman` exclusively** (Docker must not be used or installed).

## Security

- CDP exposes the logged-in browser session. Keep debugging on loopback only (`127.0.0.1`).
- Redact sensitive values (`token`, `auth`, `api_key`, `session`, `invite`, etc.) in any output via `sweep_lib.redact_url()` — best-effort across query, fragment, userinfo, and token-shaped path segments, never a proven-safe guarantee.
- Delete scratch artifacts (`$SCRATCH/cdp*.json`, Firefox `$SCRATCH/*.copy.jsonlz4`, dev logs) at the end of every run. See `references/dev-mode.md` → Cleanup.
- Never commit `.env`, tokens, session files, or scratch captures.
