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


# ---------------------------------------------------------------------------
# Per-domain concurrency (customer feedback #4)
#
# A single global pool sends every concurrent fetch at one host: 8 Medium tabs
# became 8 simultaneous requests to Medium, which is how you collect a 429 and a
# temporary ban. Concurrency must be keyed per host.
# ---------------------------------------------------------------------------

def _peak_per_host(urls, gates):
    """Run `urls` through `gates` and return the peak simultaneous count/host."""
    import threading
    import time
    import concurrent.futures as cf
    live, peak, lock = {}, {}, threading.Lock()

    def slow(url, **kw):
        h = fetch_articles.host_of(url) or url
        with lock:
            live[h] = live.get(h, 0) + 1
            peak[h] = max(peak.get(h, 0), live[h])   # recorded, never decremented
        time.sleep(0.02)
        with lock:
            live[h] -= 1
        return {"url": url}

    monkey = fetch_articles.fetch_one
    fetch_articles.fetch_one = slow
    try:
        with cf.ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(lambda u: gates.run(u, lambda x: slow(x)), urls))
    finally:
        fetch_articles.fetch_one = monkey
    return peak


@pytest.mark.parametrize("url,expect", [
    ("https://medium.com/@a/p", "medium.com"),
    ("https://www.medium.com/@a/p", "medium.com"),   # www stripped
    ("https://MEDIUM.com/@a/p", "medium.com"),       # case-folded
    ("https://sub.example.co.uk/x", "sub.example.co.uk"),
    ("not a url", ""),                                 # unparseable -> caller falls back
])
def test_host_of_normalises(url, expect):
    assert fetch_articles.host_of(url) == expect


def test_per_domain_cap_holds_under_concurrency():
    urls = ([f"https://medium.com/@a/p{i}" for i in range(8)]
            + [f"https://ex{i}.com/blog/p" for i in range(12)])
    peak = _peak_per_host(urls, fetch_articles._DomainGates(2))
    assert max(peak.values()) <= 2, f"per-domain cap violated: {peak}"


def test_ungated_concurrency_would_violate_the_cap():
    # Guards against the cap being vacuous: without gates the same workload
    # really does pile onto one host.
    class _Open:
        def run(self, u, fn):
            return fn(u)

    urls = [f"https://medium.com/@a/p{i}" for i in range(8)]
    peak = _peak_per_host(urls, _Open())
    assert max(peak.values()) > 2


def test_distinct_hosts_still_run_in_parallel():
    urls = [f"https://ex{i}.com/blog/p" for i in range(8)]
    peak = _peak_per_host(urls, fetch_articles._DomainGates(2))
    # 8 different hosts, cap 2 each -> more than one host active at once.
    assert len(peak) == 8
    assert sum(peak.values()) > 2


def test_cli_rejects_zero_per_domain(tmp_path):
    inp = tmp_path / "in.jsonl"
    inp.write_text("")
    proc = subprocess.run([PY, str(SCRIPT), "--per-domain", "0"],
                          stdin=inp.open(), capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    assert "per-domain" in proc.stderr.lower()


def test_cli_accepts_per_domain_flag(tmp_path):
    inp = tmp_path / "in.jsonl"
    inp.write_text("https://medium.com/x\n")
    proc = subprocess.run(
        [PY, str(SCRIPT), "--concurrency", "4", "--per-domain", "1"],
        stdin=inp.open(), capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, proc.stderr
