"""Behavioral tests for fetch_articles.py (mocked urllib, no network)."""
import json
import time
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "article-sweeper" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fetch_articles  # noqa: E402
from fetch_articles import (  # noqa: E402
    DEFAULT_BACKOFF_CAP,
    backoff_delay,
    is_retryable_status,
)

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


# ---------------------------------------------------------------------------
# --with-text: one request serves both classification and summarization
# ---------------------------------------------------------------------------

def test_with_text_emits_text_and_signals_from_the_same_request(monkeypatch):
    body = (b"<html><body><article>" + b"".join(
        b"<p>The committee reviewed the quarterly infrastructure budget and found "
        b"that deployment frequency increased substantially while mean time to "
        b"recovery improved across every regional data centre. </p>"
        for _ in range(8)) + b"</article></body></html>")
    calls = []

    def counting(*a, **k):
        calls.append(1)
        return _Resp(body)

    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen", counting)
    out = fetch_articles.fetch_one("https://ex.com/blog/p", timeout=5,
                                   allow_search_fallback=False, want_text=True)
    assert out["status"] == "ok"
    assert out["signals"]["words"] > 0
    assert len(out["text"]) > 0
    # The whole point: no second request.
    assert len(calls) == 1


def test_without_text_no_body_is_returned(monkeypatch):
    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b"<html>x</html>"))
    out = fetch_articles.fetch_one("https://ex.com/blog/p", timeout=5,
                                   allow_search_fallback=False)
    assert "text" not in out and "signals" not in out


def test_text_extraction_failure_degrades_gracefully(monkeypatch):
    class _Bad:
        headers = {"Content-Type": "text/html"}
        status = 200
        def read(self, n=-1): return b"\xff\xfe binary-ish"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen",
                        lambda *a, **k: _Bad())
    out = fetch_articles.fetch_one("https://ex.com/blog/p", timeout=5,
                                   allow_search_fallback=False, want_text=True)
    assert out["status"] == "ok"
    assert isinstance(out.get("text", ""), str)


def test_paywalled_skips_the_fetch_even_with_text_requested(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("paywalled must not be fetched")

    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen", explode)
    out = fetch_articles.fetch_one("https://medium.com/x", timeout=5,
                                   allow_search_fallback=False, want_text=True)
    assert out["status"] == "skipped-paywalled"
    assert "text" not in out


def test_cli_with_text_flag_emits_signals(tmp_path):
    inp = tmp_path / "in.jsonl"
    inp.write_text("https://example.com/\n")
    proc = subprocess.run(
        [PY, str(SCRIPT), "--with-text", "--concurrency", "2", "--timeout", "10"],
        stdin=inp.open(), capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, proc.stderr
    row = json.loads(proc.stdout.splitlines()[0])
    assert row["status"] == "ok"
    assert "signals" in row and "text" in row


# --- retry / backoff -------------------------------------------------------

def _http_error(code, headers=None):
    return HTTPError("u", code, "boom", headers or {}, None)


def test_is_retryable_status_covers_transient_only():
    assert all(is_retryable_status(c) for c in (408, 425, 429, 500, 502, 503, 504))
    # Refusals must not be retried: it is pointless and rude to the publisher.
    assert not any(is_retryable_status(c) for c in (401, 402, 403, 404, 451))
    assert not is_retryable_status(None)
    assert not is_retryable_status("nonsense")


def test_backoff_grows_and_is_capped():
    d1 = [backoff_delay(1, base=0.5, cap=4) for _ in range(40)]
    d3 = [backoff_delay(3, base=0.5, cap=4) for _ in range(40)]
    assert max(d1) <= 0.5 and min(d1) >= 0.25
    assert max(d3) <= 4.0
    assert max(d3) > max(d1)
    assert backoff_delay(99, base=0.5, cap=4) <= 4.0


def test_backoff_jitter_actually_varies():
    """Without spread, N tabs on one host retry in lockstep and re-stampede."""
    assert len({backoff_delay(2) for _ in range(50)}) > 1


def test_backoff_honours_retry_after():
    assert backoff_delay(1, retry_after="2") == 2.0
    # A junk header must not crash the retry path.
    assert 0 <= backoff_delay(1, retry_after="soon") <= DEFAULT_BACKOFF_CAP


def test_fetch_one_retries_429_then_succeeds(monkeypatch):
    seq = [_http_error(429), _http_error(429), _Resp(b"<html>body</html>")]
    calls = []

    def fake(req, timeout=None):
        calls.append(1)
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen", fake)
    monkeypatch.setattr(fetch_articles.time, "sleep", lambda s: None)
    out = fetch_articles.fetch_one("https://ex.com/a", timeout=1, allow_search_fallback=False)
    assert out["status"] == "ok" and out["attempts"] == 3 and len(calls) == 3


def test_fetch_one_retries_transport_errors(monkeypatch):
    def fake(req, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise fetch_articles.urllib.error.URLError("conn reset")
        return _Resp(b"body")

    calls = []
    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen",
                        lambda req, timeout=None: fake(req, timeout))
    monkeypatch.setattr(fetch_articles.time, "sleep", lambda s: None)
    out = fetch_articles.fetch_one("https://ex.com/b", timeout=1, allow_search_fallback=False)
    assert out["status"] == "ok" and out["attempts"] == 3


def test_fetch_one_does_not_retry_403(monkeypatch):
    calls = []

    def fake(req, timeout=None):
        calls.append(1)
        raise _http_error(403)

    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen", fake)
    monkeypatch.setattr(fetch_articles.time, "sleep", lambda s: None)
    out = fetch_articles.fetch_one("https://ex.com/c", timeout=1, allow_search_fallback=False)
    assert out["status"] == "blocked" and len(calls) == 1, "403 must fail on first try"


def test_fetch_one_gives_up_after_max_attempts(monkeypatch):
    calls = []

    def fake(req, timeout=None):
        calls.append(1)
        raise _http_error(503)

    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen", fake)
    monkeypatch.setattr(fetch_articles.time, "sleep", lambda s: None)
    out = fetch_articles.fetch_one("https://ex.com/d", timeout=1, allow_search_fallback=False,
                    max_attempts=3)
    # Exhausted 503 is a plain error; only 429 stays "throttled" so the
    # summary line can say "rate limited" rather than "fetch failed".
    assert out["status"] == "error" and out["http"] == 503
    assert out["attempts"] == 3 and len(calls) == 3


def test_max_attempts_one_disables_retry(monkeypatch):
    calls = []

    def fake(req, timeout=None):
        calls.append(1)
        raise _http_error(500)

    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen", fake)
    monkeypatch.setattr(fetch_articles.time, "sleep", lambda s: None)
    out = fetch_articles.fetch_one("https://ex.com/e", timeout=1, allow_search_fallback=False,
                    max_attempts=1)
    assert out["status"] == "error" and len(calls) == 1


def test_paywalled_skip_never_makes_a_request(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1))
    out = fetch_articles.fetch_one("https://www.nytimes.com/2026/x.html", timeout=1, allow_search_fallback=False)
    assert out["status"] == "skipped-paywalled" and out["attempts"] == 0
    assert not calls


def test_every_outcome_reports_attempts(monkeypatch):
    """Callers count retries from the field; a missing key would KeyError."""
    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(b"x"))
    ok = fetch_articles.fetch_one("https://ex.com/f", timeout=1, allow_search_fallback=False)
    assert ok["attempts"] == 1


def test_exhausted_429_still_reports_throttled(monkeypatch):
    """The distinction survives the retry loop, not just the first failure."""
    monkeypatch.setattr(fetch_articles.urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(
                            _http_error(429)))
    monkeypatch.setattr(fetch_articles.time, "sleep", lambda s: None)
    out = fetch_articles.fetch_one("https://ex.com/g", timeout=1,
                                   allow_search_fallback=False)
    assert out["status"] == "throttled" and out["attempts"] == 3


# --- host-level backoff: the documented consequence of the per-domain cap ---

def _concurrent_probe(per_domain, hosts_urls, max_workers, sleep_s=0.05):
    """Run URLs through the real gates, tracking true peak in-flight per host.

    The real sleep is deliberately NOT patched away: the whole point is to
    observe the window while a URL is parked in backoff, which is exactly the
    window a no-op sleep would erase.
    """
    import threading as _th
    inflight, peak, lock = {}, {}, _th.Lock()

    def fake(req, timeout=None):
        host = req.full_url.split("/")[2]
        with lock:
            inflight[host] = inflight.get(host, 0) + 1
            peak[host] = max(peak.get(host, 0), inflight[host])
        try:
            time.sleep(sleep_s)          # stands in for the backoff park
            if inflight[host] < 3:
                raise _http_error(429, {"Retry-After": "0"})
            return _Resp(b"ok")
        finally:
            with lock:
                inflight[host] -= 1

    monkey = fetch_articles.urllib.request
    orig = monkey.urlopen
    monkey.urlopen = fake
    try:
        gates = fetch_articles._DomainGates(per_domain)
        with fetch_articles.ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(lambda u: gates.run(u, lambda x: fetch_articles.fetch_one(
                x, timeout=1, allow_search_fallback=False, max_attempts=4)),
                hosts_urls))
    finally:
        monkey.urlopen = orig
    return peak


def test_backoff_park_still_counts_against_the_host_cap(monkeypatch):
    """A URL parked in backoff holds its host slot.

    fetch_one() sleeps inside the per-domain gate, so a throttled URL keeps
    the host's semaphore held while it waits. That is deliberate: a 429 means
    the host wants slower traffic, so the penalty lands on the host that caused
    it instead of on unrelated hosts.
    """
    urls = [f"https://slow.example/{i}" for i in range(6)]
    peak = _concurrent_probe(2, urls, max_workers=6)
    assert max(peak.values()) <= 2, f"host cap violated while parked: {peak}"


def test_backoff_park_is_released_so_later_urls_proceed(monkeypatch):
    """The cap must throttle, not deadlock: every URL still gets served."""
    urls = [f"https://slow.example/{i}" for i in range(4)]
    peak = _concurrent_probe(2, urls, max_workers=4)
    assert peak, "probe must observe traffic"
    assert max(peak.values()) <= 2
