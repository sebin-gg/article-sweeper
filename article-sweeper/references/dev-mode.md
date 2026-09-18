# Dev mode restarts

Read this file only when the browser needs a restart to enable remote
debugging.

## Before any kill

Back up `Sessions/` plus `Preferences` plus the summary file to a dated
backup dir. Confirm `restore_on_startup == 1` in `Preferences`. Without it,
tabs do not come back.

## Launch per OS

```bash
# Linux (X11/XWayland)
DISPLAY=:0 nohup <browser-binary> --ozone-platform=x11 --remote-debugging-port=9222 >$SCRATCH/<browser>-dev.log 2>&1 &
# macOS (no DISPLAY, no ozone flag)
/Applications/<Browser>.app/Contents/MacOS/<binary> --remote-debugging-port=9222 &
# Windows (cmd)
"C:\Program Files\<Browser>\<binary>.exe" --remote-debugging-port=9222
curl -s http://127.0.0.1:9222/json/version
```

`$SCRATCH` is `/tmp/opencode` on Linux and macOS,
`%TEMP%\opencode` on Windows.

## Rules

Kill the main browser PID with SIGTERM and wait for exit. Clear stale
`SingletonLock` or `SingletonSocket` only when the browser fails to start.

Never use `--auto-open-devtools-for-tabs` persistently. It opens one
DevTools window per tab. When it was used, close all `devtools://` targets
afterwards and restart with only `--remote-debugging-port=9222`.

After restart, verify the summarized URLs are gone and the remaining page
count matches expectation.
