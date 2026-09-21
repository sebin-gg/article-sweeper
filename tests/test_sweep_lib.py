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
    cdp_url,
    check_endpoint_identity,
    classify_url,
    cleanup_session_copy,
    copy_session_safe,
    decode_mozlz4,
    dedupe_key,
    dedupe_tabs,
    diff_tab_sets,
    endpoint_for,
    is_internal_target,
    parse_cdp_list,
    pick_current_entry,
    recount_and_fix_header,
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


def test_redact_masks_fragment_userinfo_and_path_token():
    frag = redact_url("https://ex.com/cb#access_token=secret123&token_type=bearer")
    assert "secret123" not in frag
    ui = redact_url("https://user:pass123@ex.com/a")
    assert "pass123" not in ui and "ex.com" in ui
    jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
           "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
    pr = redact_url(f"https://ex.com/invite/{jwt}")
    assert jwt not in pr and "***" in pr
    # human slugs stay readable
    assert "why-x" in redact_url("https://ex.com/blog/why-x")


def test_check_endpoint_identity_matches_and_rejects():
    good = {"Browser": "Chrome/136.0.0.0", "Protocol-Version": "1.3"}
    out = check_endpoint_identity(
        "127.0.0.1", 9224, expect_browser="chrome",
        fetch_version=lambda h, p: good)
    assert out["Browser"].startswith("Chrome")
    with pytest.raises(ValueError):
        check_endpoint_identity(
            "127.0.0.1", 9224, expect_browser="brave",
            fetch_version=lambda h, p: good)
    with pytest.raises(ValueError):
        check_endpoint_identity(
            "127.0.0.1", 9224, expect_browser="",
            fetch_version=lambda h, p: ["not-a-dict"])
    with pytest.raises(ValueError):
        def _boom(h, p):
            raise RuntimeError("down")
        check_endpoint_identity("127.0.0.1", 9224, fetch_version=_boom)


def test_cdp_url_brackets_ipv6():
    assert cdp_url("127.0.0.1", 9222, "/json/list") == \
        "http://127.0.0.1:9222/json/list"
    assert cdp_url("::1", 9222, "/json/list") == \
        "http://[::1]:9222/json/list"
    assert cdp_url("localhost", "9224", "/json/version") == \
        "http://127.0.0.1:9224/json/version"


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


def test_recount_and_fix_header_rewrites_stale_count(tmp_path):
    f = tmp_path / "summary.txt"
    f.write_text(
        "# head\n\n5 article tabs summarised. Non-article tabs left open.\n\n"
        "## A\nLink: x\n---\n## B\nLink: y\n---\n",
        encoding="utf-8")
    assert recount_and_fix_header(f) == 2
    text = f.read_text(encoding="utf-8")
    assert "2 article tabs summarised." in text
    assert "5 article tabs summarised." not in text
    assert recount_entries(f) == 2


def test_recount_and_fix_header_no_header_untouched(tmp_path):
    g = tmp_path / "noheader.txt"
    g.write_text("## X\n", encoding="utf-8")
    assert recount_and_fix_header(g) == 1
    assert g.read_text(encoding="utf-8") == "## X\n"
    assert recount_and_fix_header(tmp_path / "missing.txt") == 0


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
    dst = copy_session_safe(src, tmp_path / "scratch", settle_ms=1)
    assert dst.is_file() and dst != src
    doc = decode_mozlz4(dst.read_bytes())
    assert doc["windows"][0]["tabs"][0]["entries"][0]["url"] == \
        "https://ex.com/old"
    with pytest.raises(ValueError):
        decode_mozlz4(b"not-a-session")
    cleanup_session_copy(dst)


def test_copy_failure_leaves_no_scratch(tmp_path, monkeypatch):
    import io
    src = tmp_path / "sessionstore.jsonlz4"
    src.write_bytes(make_session_bytes())
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    calls = {"n": 0}

    real_open = open

    def flaky(path, *a, **k):
        # alternate bytes on every read of src so stability never holds
        if str(path) == str(src) and "rb" in (a[0] if a else k.get("mode", "")):
            calls["n"] += 1
            return io.BytesIO(b"a" if calls["n"] % 2 else b"b")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", flaky)
    import sweep_lib
    with pytest.raises(ValueError):
        sweep_lib.copy_session_safe(src, scratch, retries=2, settle_ms=0)
    assert list(scratch.glob("*")) == []


def test_copy_stable_and_cleanup(tmp_path):
    src = tmp_path / "sessionstore.jsonlz4"
    src.write_bytes(make_session_bytes())
    dst = copy_session_safe(src, tmp_path / "scratch", settle_ms=1)
    assert dst.is_file() and dst != src
    cleanup_session_copy(dst)
    assert not dst.exists()


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


def test_diff_uses_fresh_baseline_not_stale_expect():
    # --expect snapshot has extra tab Z that closed naturally before the
    # fresh live list; diffing live-vs-after must not flag Z unexpected.
    mk = lambda i: TabRecord(id=i, url=f"https://ex.com/{i}",
                             title=i, endpoint="e", browser="b")
    expect = [mk("A"), mk("Z")]
    live = [mk("A")]
    after = []
    d = diff_tab_sets(live, after, {"A"})
    assert d["unexpectedly_closed"] == []
    # but diffing stale expect-vs-after WOULD misreport Z:
    d2 = diff_tab_sets(expect, after, {"A"})
    assert d2["unexpectedly_closed"] == ["Z"]


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


def test_close_cli_requires_expect_and_validates(tmp_path):
    ids = tmp_path / "ids.txt"
    ids.write_text("A\n", encoding="utf-8")
    # --expect missing entirely => argparse error, no blind close
    r = run("cdp_close.py", str(ids), "--port", "99999")
    assert r.returncode != 0 and "--expect" in (r.stderr + r.stdout)
    # --expect present but bad port still rejected
    exp = tmp_path / "exp.json"
    exp.write_text("[]", encoding="utf-8")
    r = run("cdp_close.py", str(ids), "--port", "99999",
            "--expect", str(exp))
    assert r.returncode != 0 and "bad port" in (r.stderr + r.stdout)
    r2 = run("cdp_close.py", str(ids), "--host", "0.0.0.0",
             "--expect", str(exp))
    assert r2.returncode != 0 and "loopback" in (r2.stderr + r2.stdout)


def test_decode_cli_refuses_no_copy_on_live_path(tmp_path):
    live = tmp_path / "profile" / "sessionstore.jsonlz4"
    live.parent.mkdir(parents=True)
    live.write_bytes(make_session_bytes())
    r = run("decode_firefox_session.py", str(live), "--no-copy")
    assert r.returncode != 0 and "--no-copy" in (r.stderr + r.stdout)
    # explicit scratch copy location passes the guard
    ok_copy = tmp_path / "opencode" / "sessionstore.copy.jsonlz4"
    ok_copy.parent.mkdir(parents=True)
    ok_copy.write_bytes(make_session_bytes())
    r2 = run("decode_firefox_session.py", str(ok_copy), "--no-copy")
    assert r2.returncode == 0


def test_decode_cli_copies_and_warns(tmp_path):
    src = tmp_path / "sessionstore.jsonlz4"
    src.write_bytes(make_session_bytes())
    r = run("decode_firefox_session.py", str(src), "--redact")
    assert r.returncode == 0
    assert "safe copy" in r.stderr
    assert "TOTAL TABS: 2" in r.stderr  # empty-entries tab skipped


def test_decode_cli_cleans_copy_on_read_failure(tmp_path, monkeypatch):
    # safe copy created -> read fails -> scratch copy still deleted
    import decode_firefox_session as dfs
    src = tmp_path / "sessionstore.jsonlz4"
    src.write_bytes(make_session_bytes())
    scratch = tmp_path / "scratch"
    real_read_bytes = Path.read_bytes
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] >= 2:  # call 1 is the copy-verify read; 2 is main's
            raise OSError("disk gone")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    with pytest.raises(SystemExit) as excinfo:
        dfs.main([str(src), "--scratch", str(scratch)])
    assert "cannot read" in str(excinfo.value)
    assert list(scratch.glob("*")) == []
