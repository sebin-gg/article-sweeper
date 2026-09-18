# article-sweeper

Use this skill when you have open article tabs to summarize, file, and close.

## Install

```bash
npx skills add <owner>/article-sweeper@article-sweeper
```

Replace `<owner>` with the repo's GitHub owner.
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

Linux stores Chromium profiles under `~/.config/<browser>/Default/`.
macOS uses `~/Library/Application Support/<browser>/Default/`.
Windows uses `%LOCALAPPDATA%\<browser>\User Data\Default\`.

Firefox profiles live under `~/.mozilla/firefox/` or
`~/.config/mozilla/firefox/` on Linux,
`~/Library/Application Support/Firefox/Profiles/` on macOS, and
`%APPDATA%\Mozilla\Firefox\Profiles\` on Windows.

## License

MIT. See LICENSE.
