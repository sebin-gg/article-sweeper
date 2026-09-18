"""Close Chromium tabs by CDP id, in parallel.

Usage:
    python3 cdp_close.py <ids.txt> [--port 9222]

ids.txt holds one tab id per line. Prints per-id results to stdout and a
summary to stderr. Exits 0 only when every id closed.
"""
import argparse
import concurrent.futures
import sys
import urllib.request


def close(port, tab_id):
    url = f"http://127.0.0.1:{port}/json/close/{tab_id}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, method="PUT"), timeout=5
        ) as resp:
            return tab_id, True, resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - report, do not crash batch
        return tab_id, False, str(exc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ids_file")
    parser.add_argument("--port", default="9222")
    args = parser.parse_args()
    with open(args.ids_file, encoding="utf-8") as fh:
        ids = [line.strip() for line in fh if line.strip()]
    ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        for tab_id, done, msg in pool.map(lambda i: close(args.port, i), ids):
            print(f"{'CLOSED' if done else 'FAILED'} {tab_id} {msg[:80]}")
            ok += done
    print(f"closed ok: {ok}/{len(ids)}", file=sys.stderr)
    sys.exit(0 if ok == len(ids) else 1)


if __name__ == "__main__":
    main()
