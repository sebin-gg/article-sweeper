# article-sweeper

<p align="center">
  <a href="https://github.com/sebin-gg/article-sweeper/actions"><img src="https://github.com/sebin-gg/article-sweeper/actions/workflows/lint.yml/badge.svg" alt="Lint"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License"></a>
  <a href="https://github.com/sebin-gg/article-sweeper/stargazers"><img src="https://img.shields.io/github/stars/sebin-gg/article-sweeper?style=social" alt="Stars"></a>
</p>

> **A tab sweeper for article hoarders.** Summarizes open article tabs across Thorium, Chromium, Chrome, Brave, Edge, Vivaldi, Opera, and Firefox, files them to a dated desktop note, and closes only the summarized tabs.
>
> Execution: local scripts by default. Browser Use or Computer Use only
> when you explicitly ask for them ("use browser use" / "use computer
> use"). A plain "summarize my tabs" request runs the script workflow.

## Highlights

- **Cross-browser**: Chromium family via CDP tab lists (verified per
  browser; derivatives treated as Chromium-compatible pending a
  list → close-one → recount check), Firefox via read-only
  `sessionstore.jsonlz4` decode (safe copy + staleness warnings).
- **Article-only**: mail, chats, repos, dashboards, trackers, and adult pages stay open, never summarized.
- **Append-only notes**: entries go to `~/Desktop/summary article YYYY-MM-DD.txt`. Creates when missing, never overwrites; atomic locked appends with an authoritative recount.
- **Safe close**: Chromium tabs revalidated (same canonical URL) immediately before close with disappearance checks; verifies the rest stayed open. Firefox tabs close by hand from a printed list.
- **Dev-mode restart**: backup first, per-browser loopback ports, session-restore check against the vendor's actual schema, no `pkill -9`, no session-file deletes. Note: Chrome 136+ needs a dedicated `--user-data-dir` for remote debugging — the live default profile cannot be CDP-attached in place.

## Install

```bash
npx skills add sebin-gg/article-sweeper@article-sweeper
```

Restart your agent session after install so the skill loads.

## Use

Example prompts:

- "Summarize my open articles"
- "Sweep my article tabs"
- "Summarize browser tabs and close the ones you summarized"
- "Sweep only Chrome and Firefox"
- "Summarize Thorium tabs, leave everything else open"

Name browsers to scope the sweep. Unnamed browsers stay open and get no
summary. No names = all detected browsers swept.

## What it does

1. It lists live tabs in Thorium, Chromium, Chrome, Brave, Edge, Vivaldi,
   Opera, and Firefox. For Chromium-family browsers it reads the tabs
   through each browser's own debugging port. For Firefox it reads a safe
   copy of `sessionstore.jsonlz4` (read-only; staleness warnings included).
2. It sorts articles from non-articles with a deterministic URL
   normalize/dedupe/classify baseline (`scripts/sweep_lib.py`) plus agent
   judgment. Mail, chats, repos, dashboards,
   trackers, and adult pages stay open and get no summary.
3. It writes one entry per article with a link, a short summary, and a
   takeaway.
4. It appends the entries to `~/Desktop/summary article YYYY-MM-DD.txt`. It
   creates the file when missing and never overwrites existing files.
   Concurrent batches append under lock; the header count is recounted
   authoritatively at the end.
5. It closes only the summarized Chromium tabs (revalidated by canonical
   URL immediately before close, disappearance confirmed) and reports what
   stayed open. Firefox tabs close by hand from the printed list.

### Example entry

```markdown
## <Title>
Link: <clean canonical URL>
Summary: <concrete facts, numbers, names — whatever length the article needs>
Takeaway: <one sentence>
---
```

## Project layout

- `article-sweeper/SKILL.md` — the skill: execution mode (scripts default), detect, enumerate, classify, summarize, append, close.
- `article-sweeper/scripts/` — `sweep_lib.py` (deterministic core: normalize, dedupe, classify, CDP validation, close verification, atomic append), `list_cdp_tabs.py`, `cdp_close.py`, `decode_firefox_session.py`.
- `article-sweeper/references/` — `chromium.md`, `firefox.md`, `dev-mode.md` per-browser details.
- `tests/` — pytest suite with mocked CDP / Firefox fixtures.
- `CHANGELOG.md` — release notes per version.

## Requirements

- One supported browser from the list above.
- `curl` and `python3` on your PATH.
- Python package `lz4` (`pip install lz4`) for Firefox session reads.
- The WebFetch and WebSearch tools in your agent for fetching and fallback.
- Remote debugging needs a per-browser loopback port; Chrome 136+ additionally
  needs a dedicated `--user-data-dir` (see `article-sweeper/references/dev-mode.md`).

## Safety

The skill backs up browser session data before any restart. It restarts a
Chromium browser only to enable its debugging port, only on its own port,
and only after confirming session restore via that vendor's actual
preference schema. It never force-kills the browser and never deletes
session files. Debugging stays on loopback only; scratch captures
(`cdp*.json`, dev logs) may contain sensitive URLs and are deleted at the
end of each run (prefer `--redact`). When it cannot reach a tab safely, it prints a close list and
leaves the tabs open. Firefox enumeration is read-only; Firefox tabs always
close by hand.

## Browsers and paths

| # | Browser | Binaries probed | Profile / session location | Tab source |
| :---: | :--- | :--- | :--- | :--- |
| 1 | [Thorium](https://thorium.rocks/) | `thorium` | `~/.config/thorium/Default/` (Linux) | CDP |
| 2 | [Chromium](https://www.chromium.org/) | `chromium`, `chromium-browser` | `~/.config/chromium/Default/` (Linux) | CDP |
| 3 | [Chrome](https://www.google.com/chrome/) | `google-chrome`, `google-chrome-stable` | `~/.config/google-chrome/Default/` (Linux) | CDP |
| 4 | [Brave](https://brave.com/) | `brave`, `brave-browser` | `~/.config/BraveSoftware/Brave-Browser/Default/` (Linux) | CDP |
| 5 | [Edge](https://www.microsoft.com/edge) | `microsoft-edge`, `microsoft-edge-stable` | `~/.config/microsoft-edge/Default/` (Linux) | CDP |
| 6 | [Vivaldi](https://vivaldi.com/) | `vivaldi` | `~/.config/vivaldi/Default/` (Linux) | CDP |
| 7 | [Opera](https://www.opera.com/) | `opera` | `~/.config/opera/Default/` (Linux) | CDP |
| 8 | [Firefox](https://www.mozilla.org/firefox/) | `firefox` | profiles under `~/.mozilla/firefox/` (Linux) | `sessionstore.jsonlz4` |

Linux stores Chromium profiles under `~/.config/<browser>/Default/`.
macOS uses `~/Library/Application Support/<browser>/Default/`.
Windows uses `%LOCALAPPDATA%\<browser>\User Data\Default\`.

Firefox profiles live under `~/.mozilla/firefox/` or
`~/.config/mozilla/firefox/` on Linux,
`~/Library/Application Support/Firefox/Profiles/` on macOS, and
`%APPDATA%\Mozilla\Firefox\Profiles\` on Windows.

### Vendor references

- [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) — `/json/list` enumeration, `/json/close/<id>` close. Tab lists (per-browser endpoints) over session files.
- [Firefox profiles — where user data is stored](https://support.mozilla.org/en-US/kb/profiles-where-firefox-stores-user-data) — profile layout, `sessionstore.jsonlz4` location.
- [Chrome 136 remote-debugging restriction](https://developer.chrome.com/docs/devtools) — `--remote-debugging-port` requires a non-default `--user-data-dir`; see `article-sweeper/references/dev-mode.md`.
- [Firefox profiles — where user data is stored](https://support.mozilla.org/en-US/kb/profiles-where-firefox-stores-user-data) — profile layout, `sessionstore.jsonlz4` location.
- Skill internals: `article-sweeper/references/chromium.md`, `article-sweeper/references/firefox.md`, `article-sweeper/references/dev-mode.md`.

## FAQ

<details>
<summary><b>Will it close my mail, chats, or repos?</b></summary>

No. Non-articles are never summarized and never closed. Only summarized article tabs close.

</details>

<details>
<summary><b>What if a page blocks direct fetch?</b></summary>

The entry says so honestly (`This summary is search based because the page blocked direct fetch.`) and falls back to WebSearch on the exact title.

</details>

<details>
<summary><b>Where do summaries go?</b></summary>

`~/Desktop/summary article YYYY-MM-DD.txt` (same basename on Windows: `$USERPROFILE\Desktop\`). One file per day, appended only — existing files never overwritten.

</details>

<details>
<summary><b>More than ~15 articles?</b></summary>

The skill splits into batches, summarizes in parallel subagents with the same entry format, then concatenates.

</details>

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) © 2026 article-sweeper contributors
