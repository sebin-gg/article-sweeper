# Dev mode restarts

Read this file only when the browser needs a restart to enable remote
debugging.

> Trust boundary: connecting CDP to a logged-in browser session exposes
> active accounts, cookies, and page content to any tooling on the
> debugging port. Keep the port on loopback only (`127.0.0.1`), never
> expose it to a network, and delete scratch captures (`$SCRATCH/cdp*.json`,
> `$SCRATCH/*.copy.jsonlz4` Firefox session copies, dev logs) at the end
> of each run. See "Cleanup" below.

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

## Chrome existing session via user consent (Chrome 144+, host-dependent)

Chrome M144+ offers a consent-based alternative that reaches the user's
*actual live tabs* instead of a second profile: the user enables Remote
Debugging at `chrome://inspect/#remote-debugging`, and Chrome shows a
permission dialog for every incoming debugging connection, plus a
"controlled by automated test software" banner while active
(see "Let your Coding Agent debug your browser session with Chrome
DevTools MCP", Chrome for Developers blog; `chrome-devtools-mcp`
`docs/advanced-usage.md`).

Limits that matter for this skill (verified from those docs, not
assumed):

- The supported client is a **host Browser Use / MCP capability**
  (e.g. `chrome-devtools-mcp --autoConnect`), not this skill's raw
  `curl`-to-CDP local scripts. Do not claim the consent flow opens a
  plain CDP HTTP port for the scripts — route it through the host
  capability, or stay on the dedicated-profile path.
- Requires Chrome ≥ 144 and a host that actually exposes the MCP/Browser
  Use connection. If either is missing, fall back to the dedicated
  profile (or session-file summary + printed close list).
- Expect friction by design: approval is per connection (no persistent
  "always allow"; a persist-permission request was declined upstream),
  parallel clients can stack multiple dialogs, and the consent banner is
  always visible. Never auto-click approvals; never treat a missing
  approval as consent.
- The permission step is a genuine trust boundary, not an annoyance:
  the session is the user's live logged-in Chrome. State that plainly
  when asking.

Suggested agent wording: "I found your live Chrome, but I can't reach
its tabs from here. If your setup includes browser automation, you can
grant it access: open `chrome://inspect/#remote-debugging`, enable
Remote Debugging, and approve Chrome's permission dialog. Otherwise
I'll use a separate debugging profile (your normal tabs stay out of
scope) or summarize without closing."

## Per-browser endpoints (no single global port)

Multiple Chromium-family browsers cannot share one debugging endpoint.
Assign one loopback port per browser (defaults; probe with
`curl -s http://127.0.0.1:<port>/json/version` and pick a free one on
collision — `sweep_lib.endpoint_for()` implements this). Port discovery
has a TOCTOU gap (another process can claim a freed port before the
browser binds it), so discovery is only a hint: always re-verify with
`--check-endpoint`/`--browser` (and `--expect-cmd` where the workflow
launched the browser itself) immediately before enumerating or closing —
a reclaimed port fails validation and the run aborts.

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
--expect <fresh-list.json>`). `/json/version` product checks prove the
browser *family* on a port, not the exact process: when this workflow
launched the browser itself, additionally pass a command-line fragment
(binary name or `--user-data-dir=…`) as `--expect-cmd` to `list_cdp_tabs.py
--check-endpoint` / `cdp_close.py` — on Linux the listening PID's command
line must contain it (`sweep_lib.verify_endpoint_process()`), otherwise
the run aborts. Off Linux the flag fails closed; fall back to the
product check plus the launch-time PID you recorded.

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

## Launch handshake (verify, then trust)

A free port at discovery time may be claimed by another process before
the browser binds it, so a launch is only complete when the endpoint
itself reports back:

1. Take candidates from `sweep_lib.iter_candidate_ports()` (preferred
   port first, every try recorded so retries never repeat).
2. Launch the browser on the candidate port.
3. Poll `sweep_lib.wait_for_endpoint(host, port, expect_browser=<name>)`
   until `/json/version` answers with the right product (or it times
   out) — this is the "launched process bound and reported success"
   signal. Then run the `--expect-cmd` process check where supported.
4. On timeout or identity mismatch: kill what you launched, take the
   next candidate, repeat. Never operate on an unverified endpoint —
   a reclaimed port fails here by design.

```bash
curl -s http://127.0.0.1:9225/json/version   # manual equivalent of step 3
```

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

`cdp.json` captures, Firefox `*.copy.jsonlz4` session copies, and dev
logs can contain tokens, document ids, invite codes, and full session
state in URLs. At the end of every run — success or abort
(`decode_firefox_session.py` deletes its own copy automatically unless
`--keep-copy` is passed):

```bash
shred -u $SCRATCH/cdp.json $SCRATCH/cdp-before.json $SCRATCH/*.copy.jsonlz4 $SCRATCH/<browser>-dev.log 2>/dev/null \
  || rm -f $SCRATCH/cdp.json $SCRATCH/cdp-before.json $SCRATCH/*.copy.jsonlz4 $SCRATCH/<browser>-dev.log
```

Prefer `list_cdp_tabs.py --redact` / `decode_firefox_session.py --redact`
whenever full URLs are not needed. Never commit scratch captures.
