# article-sweeper

<p align="center">
  <a href="https://github.com/sebin-gg/article-sweeper/actions"><img src="https://github.com/sebin-gg/article-sweeper/actions/workflows/lint.yml/badge.svg" alt="Lint"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License"></a>
  <a href="https://github.com/sebin-gg/article-sweeper/stargazers"><img src="https://img.shields.io/github/stars/sebin-gg/article-sweeper?style=social" alt="Stars"></a>
</p>

> **The zero-config tab sweeper for article hoarders.** Summarizes open article tabs across Thorium, Chromium, Chrome, Brave, Edge, Vivaldi, Opera, and Firefox, files them to a dated desktop note, and closes only the summarized tabs.

## Highlights

- **Cross-browser**: Chromium family via CDP ground truth, Firefox via `sessionstore.jsonlz4` decode.
- **Article-only**: mail, chats, repos, dashboards, trackers, and adult pages stay open, never summarized.
- **Append-only notes**: entries go to `~/Desktop/summary article YYYY-MM-DD.txt`. Creates when missing, never overwrites.
- **Safe close**: closes only summarized tab ids, verifies the rest stayed open. Firefox tabs close by hand from a printed list.
- **Dev-mode restart**: backup first, session-restore check, no `pkill -9`, no session-file deletes.

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

## What it does

1. It lists live tabs in Thorium, Chromium, Chrome, Brave, Edge, Vivaldi,
   Opera, and Firefox. For Chromium-family browsers it reads the tabs
   through the debugging port. For Firefox it reads a copy of
   `sessionstore.jsonlz4`.
2. It sorts articles from non-articles. Mail, chats, repos, dashboards,
   trackers, and adult pages stay open and get no summary.
3. It writes one entry per article with a link, a short summary, and a
   takeaway.
4. It appends the entries to `~/Desktop/summary article YYYY-MM-DD.txt`. It
   creates the file when missing and never overwrites existing files.
5. It closes only the summarized tabs and reports what stayed open.

### Example entry

```markdown
## <Title>
Link: <clean canonical URL>
Summary: <3-6 sentences, concrete facts, numbers, names>
Takeaway: <one sentence>
---
```

## Project layout

- `article-sweeper/SKILL.md` — the skill: detect, enumerate, classify, summarize, append, close.
- `article-sweeper/scripts/` — `list_cdp_tabs.py`, `cdp_close.py`, `decode_firefox_session.py`.
- `article-sweeper/references/` — `chromium.md`, `firefox.md`, `dev-mode.md` per-browser details.
- `CHANGELOG.md` — release notes per version.

## Requirements

- One supported browser from the list above.
- `curl` and `python3` on your PATH.
- `lz4cat` for offline Firefox session reads.
- The WebFetch and WebSearch tools in your agent for fetching and fallback.

## Safety

The skill backs up browser session data before any restart. It restarts the
browser only to enable the debugging port, and only after confirming that
session restore is on. It never force-kills the browser and never deletes
session files. When it cannot reach a tab safely, it prints a close list and
leaves the tabs open.

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

- [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) — `/json/list` enumeration, `/json/close/<id>` close. Ground truth over session files.
- [Firefox profiles — where user data is stored](https://support.mozilla.org/en-US/kb/profiles-where-firefox-stores-user-data) — profile layout, `sessionstore.jsonlz4` location.
- Skill internals: `article-sweeper/references/chromium.md`, `article-sweeper/references/firefox.md`, `article-sweeper/references/dev-mode.md`.

## FAQ

- **Will it close my mail, chats, or repos?**
  No. Non-articles are never summarized and never closed.
- **What if a page blocks direct fetch?**
  The entry says so honestly (`This summary is search based because the page blocked direct fetch.`) and falls back to WebSearch on the exact title.
- **Where do summaries go?**
  `~/Desktop/summary article YYYY-MM-DD.txt` (same basename on Windows: `$USERPROFILE\Desktop\`). One file per day, appended only.
- **More than ~15 articles?**
  The skill splits into batches, summarizes in parallel subagents with the same entry format, then concatenates.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) © 2026 browser-article-sweeper contributors
