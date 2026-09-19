---
name: article-sweeper
description: Summarize open article tabs in Thorium, Chromium, Chrome, Brave, Edge, Firefox and other browsers, append summaries to a dated desktop file without overwriting, close only summarized tabs, leave everything else open, restart browser in dev mode. Use when user says summarize open articles, summarize browser tabs, sweep tabs, article summaries, close summarized tabs, open browser in dev mode.
license: MIT
allowed-tools: ["Bash", "Read", "Edit", "Write", "Task", "WebFetch", "WebSearch"]
metadata:
  version: 1.0.1
  tags: ["browser", "tabs", "summarize", "thorium", "chromium", "firefox"]
---

# Browser Article Sweeper

When invoked:

1. Detect browsers, pick Chromium or Firefox reference.
2. Enumerate live tabs. Prefer CDP to session files.
3. Classify each tab: article or leave-open. Dedupe tracking wrappers.
4. Summarize each unique article. Fixed entry format.
5. Append entries to today's file. Modify no other files.
6. Close only summarized tabs. Verify rest stayed open.

## 0. Requirements and portability

Requires: Chromium-family browser and/or Firefox, `curl`, `python3`.
Optional: `lz4cat` (Firefox offline decode), WebFetch/WebSearch tools.

Scratch dir: `/tmp/opencode` Linux/macOS. Windows: `%TEMP%\opencode`
(Git Bash: `/tmp/opencode` also works). Read every `/tmp/opencode` path
below as `$SCRATCH` on Windows.

Desktop file: `~/Desktop/` Linux/macOS. Windows: `$USERPROFILE\Desktop\`.
Same `summary article YYYY-MM-DD.txt` basename all systems.

## 1. Detect browsers

Check binaries first, then read matching reference file. Never assume
Thorium-only.

```bash
for b in thorium chromium chromium-browser google-chrome google-chrome-stable brave brave-browser microsoft-edge microsoft-edge-stable vivaldi opera firefox; do
  command -v "$b" >/dev/null 2>&1 && echo "BIN $b $(command -v $b)"
done
```

- Chromium family (Thorium, Chromium, Chrome, Brave, Edge, Vivaldi, Opera):
  read `references/chromium.md` for session paths, close flow.
- Firefox: read `references/firefox.md` for profile paths, decode script.
  `references/dev-mode.md` covers restarts for both families.

## 2. Enumerate live tabs (preferred)

Browser running with `--remote-debugging-port=9222`: CDP page list is ground
truth. Session files mix live tabs with per-tab navigation history, so
`strings | grep '^https://'` overcounts.

```bash
curl -s http://127.0.0.1:9222/json/list > $SCRATCH/cdp.json
python3 scripts/list_cdp_tabs.py $SCRATCH/cdp.json
```

Firefox offline: `python3 scripts/decode_firefox_session.py <session-copy>`.
See `references/chromium.md` and `references/firefox.md` for details.

## 3. Classify tabs as article or leave-open

Unwrap tracking wrappers first (`google.com/url?q=`, `tracking.tldrnewsletter.com/CL0/...`,
`tracking.inflection.io`, kit-mail `lmu...` paths), then dedup by
`scheme://host + path` (drop `utm_*`, fragments).

Summarize: news posts, blog posts, docs, papers, release notes, tutorials.

Never summarize, never close:

- webmail, chats, calendars, drives, dashboards, repos, app/product homepages
- trackers, auth flows, extension-blocked pages, `devtools://`, `chrome://`
- adult pages, social feeds (summarize only a post if the user names it)
- status pages, checklists/tools that are apps, not articles

Duplicates of one article (AMP, `?sk=`, author subdomains) collapse to one
entry. Record every duplicate tab id: all of them close later.

## 4. Summarize

One entry per unique article. Plain sentences, no filler, summary as long as the article needs:

```markdown
## <Title>
Link: <clean canonical URL>
Summary: <concrete facts, numbers, names — whatever length the article needs>
Takeaway: <one sentence>
---
```

Fetch with WebFetch (markdown). Medium, some Cloudflare sites, paywalled
outlets (WSJ, Bloomberg, NYT) often return 403. Then use WebSearch on exact
title, mark entry honestly:

`This summary is search based because the page blocked direct fetch.`

More than ~15 articles: split into batches, summarize batches in parallel
subagents with exact format above, then concatenate.

## 5. Append-only summary file

Today's file: `~/Desktop/summary article YYYY-MM-DD.txt`
(same naming as prior runs, e.g. `summary article 2026-09-18.txt`).

- Never overwrite. Never modify other dated files.
- Create missing file with header:

```markdown
# Article summaries - <browser> open tabs

Saved: YYYY-MM-DD. Source: <browser + session/CDP source>. Style: plain sentences, no filler.
N article tabs summarised. Non-article tabs left open by rule (...).

Protected / left open (non-articles, not summarised):
- ...
```

- Append each batch with Edit (Read file first), updating header count.
  Verify with `grep -c '^## ' <file>`.

## 6. Close only summarized tabs

Chromium: `python3 scripts/cdp_close.py <ids.txt>`. One PUT per tab id,
20 parallel workers, 5s timeout. Verify zero ids from close set remain,
recount pages.

Firefox has no CDP close-by-id equivalent in this flow. Close summarized
Firefox tabs by hand from printed close list. Never kill renderer
processes: one process hosts many tabs.

## 7. Dev mode

Dev mode means running with remote debugging on. Read
`references/dev-mode.md` for backup rule, per-OS launch commands,
`--auto-open-devtools-for-tabs` warning. Confirm
`restore_on_startup == 1` before any restart.

## 8. Safety

- Backup before kill. Kill is data-loss-adjacent (form input, unsubmitted
  work) with session restore on.
- Never `pkill -9` browser. Never delete session files.
- CDP unreachable and user will not approve restart: summarize, print exact
  close list, leave all tabs open.
- Report at end: file path + entry count, closed count, remaining page
  count, dev-mode endpoint, backup path.
