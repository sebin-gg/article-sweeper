# Firefox tabs: enumerate

Read this file only when the target browser is Firefox.

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

Copy first, never read the live file. Run:

```bash
python3 scripts/decode_firefox_session.py <profile>/sessionstore.jsonlz4
```

The script prefers the `lz4` python package (`pip install lz4`) and prints
the live tab URL and title per line. Without it, the script tries a `tail`
plus `lz4cat` fallback that often fails on raw lz4 blocks. Then install the
package.

## Close

Firefox has no CDP close-by-id equivalent in this flow. Close summarized
Firefox tabs by hand from the printed close list, or through a Marionette or
WebDriver script when the user has one configured. Never kill renderer
processes to close tabs. One process hosts many tabs.
