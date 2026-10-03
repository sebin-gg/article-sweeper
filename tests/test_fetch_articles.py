"""Behavioral tests for fetch_articles.py (mocked urllib, no network)."""
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "article-sweeper" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fetch_articles  # noqa: E402

PY = sys.executable
SCRIPT = SCRIPTS / "fetch_articles.py"


class _Resp:
    def __init__(self, body=b"<html>hi</html>", status=200, ctype="text/html"):
        self._body, self.status, self.headers = body, status, {"Content-Type": ctype}

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_ok_fetch_reports_status_bytes_and_no_search(monkeypatch):
    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b"x" * 512))
    out = fetch_articles.fetch_one("https://example.com/blog/x", timeout=5,
                                   allow_search_fallback=False)
    assert out["status"] == "ok"
    assert out["http"] == 200
    assert out["bytes"] == 512
    assert out["needs_search"] is False


def test_403_is_blocked_and_routes_to_search_only_when_planned(monkeypatch):
    def boom(*a, **k):
        raise HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen", boom)
    # Plain host, no fallback promised -> blocked but NOT searched.
    out = fetch_articles.fetch_one("https://example.com/x", timeout=5,
                                   allow_search_fallback=False)
    assert out["status"] == "blocked"
    assert out["http"] == 403
    assert out["needs_search"] is False
    # Paywalled host with the opt-in -> routes to search.
    out = fetch_articles.fetch_one("https://medium.com/x", timeout=5,
                                   allow_search_fallback=True)
    assert out["status"] == "blocked"
    assert out["needs_search"] is True


def test_404_is_an_error_not_a_block(monkeypatch):
    def boom(*a, **k):
        raise HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen", boom)
    out = fetch_articles.fetch_one("https://example.com/gone", timeout=5,
                                   allow_search_fallback=False)
    assert out["status"] == "error"
    assert out["needs_search"] is False


def test_paywalled_is_not_fetched_at_all_by_default(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("paywalled URL must not be fetched by default")

    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen", explode)
    out = fetch_articles.fetch_one("https://medium.com/@a/s", timeout=5,
                                   allow_search_fallback=False)
    assert out["status"] == "skipped-paywalled"
    assert out["needs_search"] is False


def test_malformed_url_does_not_raise(monkeypatch):
    # urllib Request() raises ValueError before urlopen; fetch_one must absorb it
    # or one junk line loses every other result in the sweep.
    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())
    out = fetch_articles.fetch_one("not-a-url{", timeout=5,
                                   allow_search_fallback=False)
    assert out["status"] == "error"
    assert out["url"] == "not-a-url{"


def test_network_failure_is_a_typed_outcome(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen", boom)
    out = fetch_articles.fetch_one("https://example.com/x", timeout=5,
                                   allow_search_fallback=False)
    assert out["status"] == "error"
    assert "connection reset" in out["reason"]


def test_cli_streams_one_json_object_per_url(tmp_path):
    inp = tmp_path / "in.jsonl"
    inp.write_text("https://medium.com/x\n{\"url\":\"https://example.com/\",\"title\":\"t\"}\n")
    proc = subprocess.run(
        [PY, str(SCRIPT), "--concurrency", "2", "--timeout", "10"],
        stdin=inp.open(), capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, proc.stderr
    rows = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    assert len(rows) == 2
    assert {r["url"] for r in rows} == {"https://medium.com/x", "https://example.com/"}
    paywalled = next(r for r in rows if r["url"] == "https://medium.com/x")
    assert paywalled["status"] == "skipped-paywalled"


def test_cli_rejects_bad_concurrency(tmp_path):
    inp = tmp_path / "in.jsonl"
    inp.write_text("")
    proc = subprocess.run([PY, str(SCRIPT), "--concurrency", "0"],
                          stdin=inp.open(), capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    assert "concurrency" in proc.stderr.lower()


def test_cli_empty_input_is_a_clean_noop(tmp_path):
    inp = tmp_path / "in.jsonl"
    inp.write_text("\n\n")
    proc = subprocess.run([PY, str(SCRIPT)], stdin=inp.open(),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_input_reader_skips_malformed_json_lines():
    rows = list(fetch_articles._read_inputs(
        iter(['{"url":"https://a.com/"}', "{bad json", "", "https://b.com/", "{}"])))
    assert rows == ["https://a.com/", "https://b.com/"]
