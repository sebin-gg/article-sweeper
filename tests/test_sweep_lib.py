"""Behavioral tests for sweep_lib + script CLIs (mocked CDP/Firefox fixtures)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "article-sweeper" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sweep_lib import (  # noqa: E402
    TabRecord,
    atomic_append,
    canonicalize_url,
    classify_url,
    copy_session_safe,
    decode_mozlz4,
    dedupe_key,
    dedupe_tabs,
    diff_tab_sets,
    endpoint_for,
    is_internal_target,
    parse_cdp_list,
    pick_current_entry,
    recount_entries,
    redact_url,
    session_freshness,
    unwrap_tracking_wrapper,
    validate_host,
    validate_port,
    verify_close_candidates,
)

PY = sys.executable


# --- ports / hosts ----------------------------------------------------------

@pytest.mark.parametrize("bad", ["0", "00000", "99999", "-1", "abc", "", "22.5"])
def test_validate_port_rejects(bad):
    with pytest.raises(ValueError):
        validate_port(bad)


@pytest.mark.parametrize("good", ["1", "9222", "65535"])
def test_validate_port_accepts(good):
    assert 1 <= validate_port(good) <= 65535


def test_validate_host_loopback_only():
    assert validate_host("127.0.0.1") == "127.0.0.1"
    assert validate_host("localhost") == "127.0.0.1"
    for bad in ["0.0.0.0", "192.168.1.2", "example.com", ""]:
        with pytest.raises(ValueError):
            validate_host(bad)


def test_per_browser_endpoints_distinct():
    eps = {b: endpoint_for(b) for b in ("thorium", "chrome", "brave")}
    ports = [p for _, p in eps.values()]
    assert len(set(ports)) == 3


# --- URLs -------------------------------------------------------------------

def test_unwrap_google_wrapper():
    real = "https://example.com/blog/post?page=2"
    wrapped = "https://www.google.com/url?q=" + real.replace("?", "%3F")
    import urllib.parse
    wrapped = "https://www.google.com/url?q=" + urllib.parse.quote(real, safe="")
    assert unwrap_tracking_wrapper(wrapped) == real


def test_canonical_drops_tracking_keeps_meaningful():
    a = canonicalize_url("https://ex.com/p?utm_source=x&fbclid=y#frag")
    assert a == "https://ex.com/p"
    b = canonicalize_url("https://ex.com/p?page=2&lang=de&utm_medium=z")
    assert "page=2" in b and "lang=de" in b and "utm_medium" not in b
    # query order is stable
    assert (canonicalize_url("https://ex.com/p?b=1&a=2") ==
            canonicalize_url("https://ex.com/p?a=2&b=1"))


def test_dedupe_merges_tracking_variants_not_content_variants():
    tabs = [{"url": "https://ex.com/a?utm_source=x"},
            {"url": "https://ex.com/a"},
            {"url": "https://ex.com/a?page=2"}]
    groups = dedupe_tabs(tabs)
    assert len(groups) == 2
    assert len(groups[dedupe_key("https://ex.com/a")]) == 2


def test_redact_masks_tokens():
    r = redact_url("https://ex.com/d?token=abc&page=2")
    assert "abc" not in r
    assert "page=2" in r


# --- classifier --------------------------------------------------------------

def test_classify_internal_and_mail():
    assert classify_url("devtools://x")[0] is False
    assert classify_url("chrome://settings")[0] is False
    assert classify_url("https://mail.google.com/mail/u/0/#inbox")[0] is False
    assert classify_url("https://github.com/o/r/pull/1")[0] is False
    assert classify_url("https://x.com/someuser")[0] is False


def test_classify_host_boundary_no_substring_false_positive():
    # "ex.com" contains "x.com" as a substring but must NOT match it
    ok, _ = classify_url("https://ex.com/blog/why-x", "Why X")
    assert ok is True
    # subdomains of blocked hosts still blocked
    assert classify_url("https://gist.github.com/u/1")[0] is False


def test_classify_article_hint():
    ok, reason = classify_url("https://ex.com/blog/why-x", "Why X")
    assert ok and reason == "article-path-hint"


# --- CDP parse ---------------------------------------------------------------

def sample_cdp():
    return [
        {"id": "A", "type": "page", "url": "https://ex.com/a", "title": "A"},
        {"id": "B", "type": "page", "url": "devtools://devtools/x",
         "title": "DT"},
        {"id": "C", "type": "background_page",
         "url": "https://ex.com/bg", "title": "BG"},
        {"id": "D", "type": "page", "url": "chrome://settings", "title": "S"},
    ]


def test_parse_filters_internal_by_type_and_scheme():
    tabs = parse_cdp_list(sample_cdp(), endpoint="127.0.0.1:9224",
                          browser="chrome")
    assert [t.id for t in tabs] == ["A"]
    assert tabs[0].endpoint == "127.0.0.1:9224"
    assert tabs[0].canonical == "https://ex.com/a"


def test_parse_rejects_malformed():
    with pytest.raises(ValueError):
        parse_cdp_list([{"type": "page", "url": "https://ex.com/"}])  # no id
    with pytest.raises(ValueError):
        parse_cdp_list([{"id": "x", "type": "page"}])  # no url
    with pytest.raises(ValueError):
        parse_cdp_list({"id": "x"})  # not a list
    assert is_internal_target("chrome-extension://abc", "page")


def test_verify_close_skips_gone_and_navigated():
    before = [TabRecord(id="A", url="https://ex.com/a",
                        endpoint="e", browser="c"),
              TabRecord(id="B", url="https://ex.com/b",
                        endpoint="e", browser="c")]
    now = [TabRecord(id="A", url="https://ex.com/a",
                     endpoint="e", browser="c"),
           TabRecord(id="B", url="https://ex.com/OTHER",
                     endpoint="e", browser="c")]
    safe, problems = verify_close_candidates(before, now)
    assert [t.id for t in safe] == ["A"]
    assert any("navigated" in p for p in problems)
    safe2, probs2 = verify_close_candidates(before, now[:1])
    assert [t.id for t in safe2] == ["A"]
    assert any("gone" in p for p in probs2)


def test_diff_tab_sets():
    b = [TabRecord(id="A", url="https://ex.com/a"),
         TabRecord(id="B", url="https://ex.com/b"),
         TabRecord(id="C", url="https://ex.com/c")]
    a = [TabRecord(id="B", url="https://ex.com/b")]
    d = diff_tab_sets(b, a, {"A", "C"})
    assert d["still_open_from_close_set"] == []
    assert d["unexpectedly_closed"] == []
    assert d["remaining"] == 1
    # something closed that was never approved -> flagged
    d2 = diff_tab_sets(b, a, {"A"})
    assert d2["unexpectedly_closed"] == ["C"]


# --- atomic append ------------------------------------------------------------

def test_atomic_append_and_recount(tmp_path):
    f = tmp_path / "summary article 2026-01-01.txt"
    atomic_append(f, ["# head\n", "## T1\nLink: x\n---\n"])
    atomic_append(f, ["## T2\nLink: y\n---\n"])
    text = f.read_text(encoding="utf-8")
    assert text.count("## T") == 2
    assert recount_entries(f) == 2


# --- Firefox ------------------------------------------------------------------

def make_session_bytes():
    import lz4.block
    doc = {"windows": [
        {"tabs": [
            {"index": 1, "entries": [
                {"url": "https://ex.com/old", "title": "old"},
                {"url": "https://ex.com/new", "title": "new"}]},
            {"entries": [{"url": "https://ex.com/only", "title": "only"}]},
            {"entries": []},
        ]}
    ]}
    return b"mozLz40\0" + lz4.block.compress(json.dumps(doc).encode())


def test_pick_current_entry_index_aware():
    tab = {"index": 1, "entries": [
        {"url": "https://a/1"}, {"url": "https://a/2"}]}
    assert pick_current_entry(tab)["url"] == "https://a/1"
    tab2 = {"entries": [{"url": "https://a/1"}, {"url": "https://a/2"}]}
    assert pick_current_entry(tab2)["url"] == "https://a/2"  # default = last
    assert pick_current_entry({"entries": []}) is None


def test_decode_and_copy_safe(tmp_path):
    src = tmp_path / "sessionstore.jsonlz4"
    src.write_bytes(make_session_bytes())
    dst = copy_session_safe(src, tmp_path / "scratch")
    assert dst.is_file() and dst != src
    doc = decode_mozlz4(dst.read_bytes())
    assert doc["windows"][0]["tabs"][0]["entries"][0]["url"] == \
        "https://ex.com/old"
    with pytest.raises(ValueError):
        decode_mozlz4(b"not-a-session")


def test_session_freshness_backup_newer(tmp_path):
    src = tmp_path / "sessionstore.jsonlz4"
    src.write_bytes(make_session_bytes())
    info = session_freshness(src)
    assert info["exists"] and not info["backup_newer"]
    bk = tmp_path / "sessionstore-backups"
    bk.mkdir()
    import time
    time.sleep(0.02)
    (bk / "recovery.jsonlz4").write_bytes(b"x")
    info2 = session_freshness(src)
    assert info2["backup_newer"]


# --- script CLIs ----------------------------------------------------------------

def run(script, *args):
    return subprocess.run([PY, str(SCRIPTS / script), *args],
                          capture_output=True, text=True, timeout=60)


def test_list_cli_rejects_malformed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"type": "page"}]), encoding="utf-8")
    r = run("list_cdp_tabs.py", str(bad))
    assert r.returncode != 0 and "invalid CDP" in (r.stderr + r.stdout)


def test_list_cli_ok_and_redact(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps([
        {"id": "A", "type": "page",
         "url": "https://ex.com/a?token=secret", "title": "A"},
        {"id": "X", "type": "page", "url": "devtools://x", "title": "D"},
    ]), encoding="utf-8")
    r = run("list_cdp_tabs.py", str(good), "--endpoint", "127.0.0.1:9224",
            "--browser", "chrome", "--redact")
    assert r.returncode == 0 and "secret" not in r.stdout
    assert "TOTAL PAGES: 1" in r.stderr


def test_close_cli_validates_port(tmp_path):
    ids = tmp_path / "ids.txt"
    ids.write_text("A\n", encoding="utf-8")
    r = run("cdp_close.py", str(ids), "--port", "99999")
    assert r.returncode != 0 and "bad port" in (r.stderr + r.stdout)
    r2 = run("cdp_close.py", str(ids), "--host", "0.0.0.0")
    assert r2.returncode != 0 and "loopback" in (r2.stderr + r2.stdout)


def test_decode_cli_copies_and_warns(tmp_path):
    src = tmp_path / "sessionstore.jsonlz4"
    src.write_bytes(make_session_bytes())
    r = run("decode_firefox_session.py", str(src), "--redact")
    assert r.returncode == 0
    assert "safe copy" in r.stderr
    assert "TOTAL TABS: 2" in r.stderr  # empty-entries tab skipped
