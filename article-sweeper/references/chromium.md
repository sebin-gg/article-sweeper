# Chromium tabs: enumerate and close

Read this file only when the target browser is Thorium, Chromium, Chrome,
Brave, Edge, Vivaldi, or Opera.

> Compatibility note: only Chromium CDP plus Firefox session parsing are
> directly implemented and tested here. Thorium, Brave, Edge, Vivaldi, and
> Opera are treated as *Chromium-compatible pending verification*: they
> usually expose the same `/json/list` + `/json/close/<id>` surface, but
> vendor binaries, profile layouts, and CDP behavior differ. Verify per
> browser (list → close one test tab → recount) before sweeping it, and
> record the result in the run report. Do not claim an unverified browser
> "works". There is no single end-to-end `sweep` executable by design:
> summarization needs agent judgment, so the repo ships deterministic
> primitives (enumerate → classify → append → close) plus the SKILL.md
> orchestration instead of one opaque command.

## Session paths

```
Linux:   ~/.config/<browser>/Default/Sessions/Session_*
         ~/.config/<browser>/Default/Sessions/Tabs_*
         ~/.config/<browser>/Default/Preferences
macOS:   ~/Library/Application Support/<browser>/Default/Sessions/Session_*
Windows: %LOCALAPPDATA%\<browser>\User Data\Default\Sessions\Session_*
```

Slug examples: `thorium`, `chromium`, `google-chrome`,
`BraveSoftware/Brave-Browser`, `microsoft-edge`. Session restore (*usually*
`restore_on_startup == 1` in `Preferences`, but verify the vendor's actual
schema per `references/dev-mode.md`) means tabs come back after a restart.

## Ground truth

`Session_*` and `Tabs_*` files mix live tabs with per-tab navigation history
(redirects, tracking wrappers). `strings <file> | grep '^https://'`
overcounts. When the browser runs with `--remote-debugging-port` on its own
per-browser port (see `references/dev-mode.md` — Chrome 136+ needs a
dedicated `--user-data-dir`), the CDP page list is ground truth:

```bash
curl -s http://127.0.0.1:<port>/json/list > $SCRATCH/cdp.json
python3 scripts/list_cdp_tabs.py $SCRATCH/cdp.json \
  --endpoint 127.0.0.1:<port> --browser <name> [--redact] \
  --host 127.0.0.1 --port <port> --check-endpoint
```

`--check-endpoint` validates `/json/version` (and the `--browser`
product match) before printing, so a reused/wrong local port cannot
lead to summarizing the wrong browser's tabs.

Filtering is by CDP target `type == "page"` plus internal-scheme exclusion
(`devtools://`, `chrome://`, `edge://`, `brave://`, `about:`, ...), done in
`sweep_lib.parse_cdp_list()` — malformed entries fail loudly instead of
being silently accepted. A bare tab id is meaningless across browsers, so
every close candidate carries its endpoint+browser identity.

## Close

Revalidate immediately before closing (tabs navigate/close/reopen between
enumeration and close), then close, then confirm disappearance. Use
`scripts/cdp_close.py` with the file of ids:

```bash
curl -s http://127.0.0.1:<port>/json/list > $SCRATCH/cdp-before.json
python3 scripts/cdp_close.py ids.txt --host 127.0.0.1 --port <port> \
  --expect $SCRATCH/cdp-before.json --browser <name> \
  --endpoint 127.0.0.1:<port>
```

The script skips ids that vanished or navigated since approval (canonical
 URL compared via `sweep_lib.verify_close_candidates()`), refuses ids not
 present in `--expect` (stale/forged lists; `--expect` and `--browser`
 are both required — no blind close-by-id path, no unattested endpoint),
 checks `/json/version` endpoint identity
via `sweep_lib.check_endpoint_identity()` before touching any tab, and
re-fetches `/json/list`
after each close instead of trusting a bare HTTP 200 — unverifiable
closes fail the run. Recount pages
afterwards with `sweep_lib.diff_tab_sets()` and confirm zero ids from the
close set remain (baseline is the fresh live list taken just before
closing, not the `--expect` authorization snapshot). `--port` must
satisfy 1–65535; `--host` is loopback-only
by design (`sweep_lib.cdp_url()` brackets `::1` correctly).
