# Chromium tabs: enumerate and close

Read this file only when the target browser is Thorium, Chromium, Chrome,
Brave, Edge, Vivaldi, or Opera.

> Compatibility note: only Chromium CDP plus Firefox session parsing are
> directly implemented and tested here. Thorium, Brave, Vivaldi, and Opera
> are *Chromium-compatible, per-browser verified as noted below*: they
> expose the same `/json/list` + `/json/close/<id>` surface, but vendor
> binaries, profile layouts, and CDP behavior differ. Verify per browser
> (list → close one test tab → recount) before sweeping it, and record
> the result in the run report. Do not claim an unverified browser
> "works".
>
> Verified 2026-09-21 (Windows 10/11, headless where noted, dedicated
> `--user-data-dir`, own loopback port, `--browser` identity match):
>
> - Chrome `Chrome/153.0.8010.53` on port 9224 — full pass: list →
>   close-one (`1/1`, exit 0) → live recount 0 pages.
> - Edge `Edg/153.0.4234.48` on port 9226 — full pass (product alias
>   `edg/` exercised).
> - Opera `OPR/136.0.0.0` (Chromium 152) on port 9228 — identity +
>   enumeration pass. **Vendor quirk:** `/json/version` reports
>   `Browser: Chrome/152.0.7977.120`; the Opera identity survives only in
>   `User-Agent` (`OPR/136.0.0.0`). `check_endpoint_identity()` handles
>   this via a conservative UA fallback (vendor-distinctive tokens only —
>   a plain-Chrome endpoint is still refused as opera). Live enumeration
>   correctly filtered internal `chrome://startpage/` targets. Close path
>   not exercised live yet (no real article tab was open).
> - Thorium `Chrome/138.0.7204.300` (Thorium 140 binary, Chromium 138
>   base) on port 9222 — full pass on Windows: identity via process
>   proof (`--expect-cmd thorium`; the product string is vendor-blind
>   plain `Chrome/...` with no Thorium token — see 1.3.7) → enumerate →
>   close-one (`1/1`, exit 0) → post-close list verified. Steady-state
>   close latency 0.02s; the WSL async-close delay did not reproduce on
>   Windows (the 1.3.4 poll guard stays). **Operational quirk:** closing
>   the browser's only page tab exits Thorium entirely — the post-close
>   recount then finds the endpoint gone (connection refused), which is
>   the expected outcome, not a failure.
> - Brave `Chrome/153.0.8010.53` (Chromium 153 base) on port 9225 —
>   full pass on Windows: vendor-blind product (plain `Chrome/...`, no
>   Brave token in Browser or UA — unlike the WSL-era builds) + process
>   proof (`--expect-cmd brave`, see 1.3.7) → close-one of two article
>   tabs (`1/1`, exit 0); bystander tab stayed open and the browser
>   alive (last-tab guard honored). Its earlier verification was
>   WSL-only (command-line fragment check).
> - Vivaldi `Chrome/8.2.4133.68` (Chromium 152 base) on port 9227 —
>   full pass: identity → enumerate → close-one (`1/1`, exit 0) → live
>   recount. **Vendor quirks (verified live, Windows):** `/json/version`
>   reports `Browser: Chrome/8.2.4133.68` — Vivaldi's *own* version
>   under a Chrome prefix — and the User-Agent is plain Chrome with no
>   vendor token, so identity requires the version-mismatch signature in
>   `sweep_lib._vivaldi_version_mismatch()`. Steady-state close latency
>   measured at 0.01s. The earlier "close hang" was root-caused to
>   Vivaldi's first-run startup: TCP accepts before `/json/version`
>   answers (see `references/dev-mode.md` launch-handshake note).
>   Also reported live: Vivaldi's "Confirm before closing multiple
>   tabs" setting (`vivaldi://settings/tabs/`) can hold a close behind
>   a UI dialog — CDP closes verified unaffected, and the tool fails
>   closed rather than clicking the dialog.
> - Opera `OPR/136.0.0.0` (Chromium 152) on port 9228 — full pass:
>   identity (UA fallback) → enumerate → close-one (`1/1`, exit 0) →
>   live recount confirms the article tab gone. `/json/version` reports
>   `Browser: Chrome/152...`; the OPR token survives only in User-Agent
>   (see 1.3.5).
>
> There is no single end-to-end `sweep` executable by design:
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
