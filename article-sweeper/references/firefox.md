# Firefox tabs: enumerate (read-only)

Read this file only when the target browser is Firefox.

> Scope: Firefox enumeration is **read-only**. This skill has no automated
> Firefox tab-close mechanism (no CDP close-by-id equivalent, no bundled
> Marionette/WebDriver flow). Close summarized Firefox tabs **by hand**
> from the printed close list, then report the summarized set and the
> closed set separately so any mismatch is visible. Do not promise
> automatic Firefox closing.

## Profiles

```
Linux:   ~/.mozilla/firefox/<profile>/  (some distros: ~/.config/mozilla/firefox/<profile>/)
macOS:   ~/Library/Application Support/Firefox/Profiles/<profile>/
Windows: %APPDATA%\Mozilla\Firefox\Profiles\<profile>\
```

Session files:

```
<profile>/sessionstore.jsonlz4            # header "mozLz40\0" plus lz4 block
<profile>/sessionstore-backups/
<profile>/places.sqlite                   # history fallback
```

## Decode

The script enforces the safety invariant itself: it **copies the session
file to scratch/temp and decodes the copy** (unless you pass `--no-copy`
with a path that already is a copy). Never point it at the live file
expecting it to copy for you — copying is the default, `--no-copy` is the
explicit opt-out for pre-made copies.

```bash
python3 scripts/decode_firefox_session.py <profile>/sessionstore.jsonlz4 [--redact]
# input already a copy:
python3 scripts/decode_firefox_session.py /tmp/session-copy.jsonlz4 --no-copy
```

Requires the `lz4` python package (`pip install lz4`). There is no
`tail`/`lz4cat` fallback — the old fallback was known-unreliable and has
been removed from the supported flow.

Staleness/freshness signals (printed to stderr, see
`sweep_lib.session_freshness()`):

- file mtime age — how old the snapshot is;
- whether a file under `sessionstore-backups/` is **newer** than
  `sessionstore.jsonlz4` (Firefox may still be running, or the main file
  may lag the backup);
- the visible tab is the tab's **selected** entry (`index`-aware via
  `sweep_lib.pick_current_entry()`), not blindly `entries[-1]`.

Firefox's session file is a continuously-persisted snapshot, not
instantaneous live ground truth. Treat decoded output as a snapshot:
if Firefox is running, confirm surprising URLs against the visible
browser before summarizing/closing.

## Close

By hand from the printed close list (see scope note above), or through a
Marionette or WebDriver script when the user has one configured. Never
kill renderer processes to close tabs. One process hosts many tabs.
