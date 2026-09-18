# Chromium tabs: enumerate and close

Read this file only when the target browser is Thorium, Chromium, Chrome,
Brave, Edge, Vivaldi, or Opera.

## Session paths

```
Linux:   ~/.config/<browser>/Default/Sessions/Session_*
         ~/.config/<browser>/Default/Sessions/Tabs_*
         ~/.config/<browser>/Default/Preferences
macOS:   ~/Library/Application Support/<browser>/Default/Sessions/Session_*
Windows: %LOCALAPPDATA%\<browser>\User Data\Default\Sessions\Session_*
```

Slug examples: `thorium`, `chromium`, `google-chrome`,
`BraveSoftware/Brave-Browser`, `microsoft-edge`. `restore_on_startup == 1`
in `Preferences` means tabs come back after a restart.

## Ground truth

`Session_*` and `Tabs_*` files mix live tabs with per-tab navigation history
(redirects, tracking wrappers). `strings <file> | grep '^https://'`
overcounts. When the browser runs with `--remote-debugging-port`, the CDP
page list is ground truth:

```bash
curl -s http://127.0.0.1:9222/json/list > $SCRATCH/cdp.json
python3 scripts/list_cdp_tabs.py $SCRATCH/cdp.json
```

## Close

One PUT per tab id, parallel workers, short timeout. Use
`scripts/cdp_close.py` with the file of ids. It prints per-id results.
Recount pages afterwards and confirm zero ids from the close set remain.
