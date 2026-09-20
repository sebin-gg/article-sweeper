# Dev mode restarts

Read this file only when the browser needs a restart to enable remote
debugging.

> Trust boundary: connecting CDP to a logged-in browser session exposes
> active accounts, cookies, and page content to any tooling on the
> debugging port. Keep the port on loopback only (`127.0.0.1`), never
> expose it to a network, and delete scratch captures (`$SCRATCH/cdp*.json`,
> dev logs) at the end of each run. See "Cleanup" below.

## Chrome 136+ restriction (important)

Since Chrome 136, Chrome ignores `--remote-debugging-port` /
`--remote-debugging-pipe` when launched against the **default** user-data
directory. Debugging requires a **non-default `--user-data-dir`**.

Consequences for this skill:

- There is no supported way to attach CDP to the user's *live default
  profile in place* by merely restarting it with a flag. That old
  procedure is obsolete and must not be attempted.
- Recommended path: ask the user to relaunch Chrome themselves with a
  dedicated debugging profile **and** turn on tab sync (or copy the
  needed tabs over), e.g.:

```bash
# Linux
google-chrome --user-data-dir="$HOME/.config/google-chrome-sweeper" \
  --remote-debugging-port=9224
```

  Then sweep the tabs visible in that debugging instance. Tabs left in
  the real default profile are out of scope for the run — report them
  as such instead of pretending to sweep them.
- Alternative when the user refuses a second profile: do **not**
  restart their browser. Summarize from a session-file copy if
  available, print the exact close list, and leave all tabs open.
- Chromium derivatives (Thorium, Brave, Edge, Vivaldi, Opera) may still
  honor the flag on the default profile today, but treat that as
  vendor-specific behavior: verify per browser, never assume.

## Per-browser endpoints (no single global port)

Multiple Chromium-family browsers cannot share one debugging endpoint.
Assign one loopback port per browser (defaults; probe with
`curl -s http://127.0.0.1:<port>/json/version` and pick a free one on
collision — `sweep_lib.endpoint_for()` implements this):

| Browser | Default port |
| :--- | :---: |
| Thorium | 9222 |
| Chromium | 9223 |
| Chrome (dedicated `--user-data-dir`, see above) | 9224 |
| Brave | 9225 |
| Edge | 9226 |
| Vivaldi | 9227 |
| Opera | 9228 |

Record which endpoint each tab id came from (`list_cdp_tabs.py
--endpoint 127.0.0.1:<port> --browser <name>`) and close only through
that same endpoint (`cdp_close.py --host 127.0.0.1 --port <port>
--expect <fresh-list.json>`).

## Before any kill

Back up `Sessions/` plus `Preferences` plus the summary file to a dated
backup dir. Confirm session restore is on before restarting — but check
the **vendor's actual preference schema**, not a universal constant:

- Chrome/Chromium: `Preferences` JSON `session.restore_on_startup == 1`.
- Brave/Edge/Vivaldi/Opera/Thorium: same Chromium preference *usually*,
  but verify the key exists in that browser's `Preferences` file; if the
  schema differs, stop and ask the user instead of assuming.
- Firefox: session restore is `browser.startup.page == 3` in
  `prefs.js` — a different model entirely.

Without confirmed restore, tabs do not come back: do not restart.

## Launch per OS (Chromium derivatives that still allow it)

```bash
# Linux (X11/XWayland) — example: Brave on its own port
DISPLAY=:0 nohup brave --remote-debugging-port=9225 >$SCRATCH/brave-dev.log 2>&1 &
# macOS (no DISPLAY, no ozone flag)
"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" --remote-debugging-port=9225 &
# Windows (cmd)
"C:\Program Files\Brave\brave.exe" --remote-debugging-port=9225
curl -s http://127.0.0.1:9225/json/version
```

`$SCRATCH` is `/tmp/opencode` on Linux and macOS,
`%TEMP%\opencode` on Windows.

## Identifying the process to restart (never guess a PID)

1. Resolve the exact binary first: `command -v <binary>` (Linux/macOS)
   or `Get-Command <binary>` (Windows).
2. List only processes whose executable path matches that binary, e.g.
   Linux: `ps -o pid=,args= -C <binary>`; Windows PowerShell:
   `Get-CimInstance Win32_Process -Filter "Name='<binary>.exe'"`.
3. Prefer the process whose command line contains the profile path
   (`--user-data-dir=...` or the `Default/` dir) for the browser in
   scope. If two candidates remain, stop and ask the user.
4. Send SIGTERM (never `pkill -9`, never bare `pkill <name>` which can
   hit unrelated browsers), wait for exit, then launch.

Clear stale `SingletonLock` or `SingletonSocket` only when the browser
fails to start.

## Rules

Never use `--auto-open-devtools-for-tabs` persistently. It opens one
DevTools window per tab. When it was used, close all `devtools://` targets
afterwards and restart with only `--remote-debugging-port=<port>`.

After restart, run the before/after set comparison
(`sweep_lib.diff_tab_sets()`): verify the summarized URLs are gone and
the remaining page count matches expectation.

## Cleanup (sensitive scratch)

`cdp.json` captures and dev logs can contain tokens, document ids, and
invite codes in URLs. At the end of every run — success or abort:

```bash
shred -u $SCRATCH/cdp.json $SCRATCH/cdp-before.json $SCRATCH/<browser>-dev.log 2>/dev/null \
  || rm -f $SCRATCH/cdp.json $SCRATCH/cdp-before.json $SCRATCH/<browser>-dev.log
```

Prefer `list_cdp_tabs.py --redact` / `decode_firefox_session.py --redact`
whenever full URLs are not needed. Never commit scratch captures.
