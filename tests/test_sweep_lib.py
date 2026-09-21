"""Behavioral tests for sweep_lib + script CLIs (mocked CDP/Firefox fixtures)."""
import json
import os
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
    browser_matches_product,
    check_endpoint_identity,
    find_pids_listening_on,
    iter_candidate_ports,
    read_process_cmdline,
    verify_endpoint_process,
    wait_for_endpoint,
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


def test_endpoint_for_accumulates_taken_set():
    taken: set[int] = set()
    _, p1 = endpoint_for("chrome", taken)
    _, p2 = endpoint_for("brave", taken)
    assert p1 in taken and p2 in taken and p1 != p2
    # preferred ports recorded too, so a later call avoids them
    assert 9224 in taken and 9225 in taken


def test_iter_candidate_ports_never_repeats():
    taken: set[int] = set()
    ports = [p for _, p in iter_candidate_ports("chrome", taken, limit=4)]
    assert len(set(ports)) == 4 and set(ports) <= taken
    assert ports[0] == 9224  # preferred first


def test_wait_for_endpoint_success_and_timeout(tmp_path):
    tabs = {"A": {"id": "A", "type": "page",
                  "url": "https://ex.com/a", "title": "A"}}
    with FakeCDP("Chrome/140.0.0.0", tabs) as cdp:
        payload = wait_for_endpoint("127.0.0.1", cdp.port,
                                    expect_browser="chrome", timeout=5)
        assert payload["Browser"].startswith("Chrome")
        with pytest.raises(ValueError):
            wait_for_endpoint("127.0.0.1", cdp.port,
                              expect_browser="brave", timeout=0.5,
                              poll_interval=0.1)
    with pytest.raises(ValueError):
        wait_for_endpoint("127.0.0.1", _closed_port(), timeout=0.5,
                          poll_interval=0.1)


# --- URLs -------------------------------------------------------------------

def test_unwrap_google_wrapper():
    real = "https://example.com/blog/post?page=2"
    wrapped = "https://www.google.com/url?q=" + real.replace("?", "%3F")
    import urllib.parse
    wrapped = "https://www.google.com/url?q=" + urllib.parse.quote(real, safe="")
    assert unwrap_tracking_wrapper(wrapped) == real


def test_unwrap_never_rewrites_plain_article_urls():
    # generic ?url=/redirect params on an article host are NOT wrappers
    plain = "https://example.com/article?url=https://other.com/x&to=y"
    assert unwrap_tracking_wrapper(plain) == plain
    redir = "https://tracker.example/click?url=https://real.com/a"
    assert unwrap_tracking_wrapper(redir) == "https://real.com/a"
    kit = "https://mail.example/lmuxyz12?u=https://real.com/b"
    assert unwrap_tracking_wrapper(kit) == "https://real.com/b"


def test_canonical_drops_tracking_keeps_meaningful():
    a = canonicalize_url("https://ex.com/p?utm_source=x&fbclid=y#frag")
    assert a == "https://ex.com/p"
    b = canonicalize_url("https://ex.com/p?page=2&lang=de&utm_medium=z")
    assert "page=2" in b and "lang=de" in b and "utm_medium" not in b
    # query order is stable
    assert (canonicalize_url("https://ex.com/p?b=1&a=2") ==
            canonicalize_url("https://ex.com/p?a=2&b=1"))
    # ref/spm/referrer are NOT universally tracking: preserved
    c = canonicalize_url("https://ex.com/p?ref=docs&spm=a.b&referrer=x")
    assert "ref=docs" in c and "spm=a.b" in c and "referrer=x" in c
    # tracking detection is case-insensitive; share tokens (?sk=) are kept
    d = canonicalize_url("https://ex.com/p?UTM_SOURCE=x&sk=abc123")
    assert "UTM_SOURCE" not in d
    assert "utm_source" not in d.lower()
    assert "sk=abc123" in d


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


def test_browser_product_aliases():
    # vendors abbreviate: Edge=Edg/, Opera=OPR/ — must still match
    assert browser_matches_product("edge", "Edg/140.0.0.0")
    assert browser_matches_product("opera", "OPR/115.0.0.0 (Opera)")
    assert browser_matches_product("chrome", "Chrome/136.0.0.0")
    assert browser_matches_product("brave", "Brave Chrome/136.0.0.0")
    assert not browser_matches_product("edge", "Chrome/136.0.0.0")
    assert not browser_matches_product("brave", "Chrome/136.0.0.0")
    assert not browser_matches_product("", "Chrome/136.0.0.0")
    out = check_endpoint_identity(
        "127.0.0.1", 9226, expect_browser="edge",
        fetch_version=lambda h, p: {"Browser": "Edg/140.0.0.0"})
    assert out["Browser"].startswith("Edg")


def test_endpoint_identity_ua_fallback_opera_reports_chrome():
    # Verified live (Windows, Opera 136): /json/version Browser field says
    # "Chrome/152..." while User-Agent keeps the OPR/ token. The UA must
    # rescue the match; without the fallback this endpoint is refused.
    opera_like = {
        "Browser": "Chrome/152.0.7977.120",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/152.0.0.0 Safari/537.36 OPR/136.0.0.0",
    }
    out = check_endpoint_identity(
        "127.0.0.1", 9228, expect_browser="opera",
        fetch_version=lambda h, p: opera_like)
    assert out["Browser"].startswith("Chrome")


def test_endpoint_identity_ua_fallback_rejects_plain_chrome_as_opera():
    # The inverse direction must NOT pass: a plain-Chrome endpoint has no
    # vendor-distinctive UA token, so it can never pose as opera (or any
    # other branded browser). This is what a naive "chrome" hint would
    # have broken.
    plain_chrome = {
        "Browser": "Chrome/152.0.7977.120",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/152.0.0.0 Safari/537.36",
    }
    for want in ("opera", "edge", "brave", "vivaldi", "thorium"):
        with pytest.raises(ValueError):
            check_endpoint_identity(
                "127.0.0.1", 9228, expect_browser=want,
                fetch_version=lambda h, p, _d=plain_chrome: _d)


def test_endpoint_identity_ua_fallback_ignores_generic_hint():
    # _ua_fallback_matches must drop generic chrome/chromium tokens: with
    # a UA that only says "chrome" (no vendor token), no branded browser
    # may match.
    from sweep_lib import _ua_fallback_matches
    ua = "Mozilla/5.0 Chrome/152.0.0.0"
    assert not _ua_fallback_matches("opera", ua)
    assert not _ua_fallback_matches("chrome", ua)  # chrome needs Browser field
    assert _ua_fallback_matches("", ua) is False


def test_vivaldi_version_mismatch_signature():
    # Verified live (Windows, Vivaldi 8.2 / Chromium 152):
    # Browser="Chrome/8.2.4133.68" (Vivaldi's own version) vs UA
    # "Chrome/152.0.0.0" (real Chromium base). No vendor token anywhere.
    from sweep_lib import _vivaldi_version_mismatch
    assert _vivaldi_version_mismatch(
        "vivaldi", "Chrome/8.2.4133.68",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
    # Same-version fields (plain Chrome) must NOT match.
    assert not _vivaldi_version_mismatch(
        "vivaldi", "Chrome/152.0.7977.120",
        "Mozilla/5.0 Chrome/152.0.0.0 Safari/537.36")
    # Missing/unparsable version on either side fails closed.
    assert not _vivaldi_version_mismatch("vivaldi", "", "Mozilla/5.0 Chrome/152.0.0.0")
    assert not _vivaldi_version_mismatch("vivaldi", "Chrome/8.2.4133.68", "")
    # Signature is exclusive to vivaldi: no other browser name may use it.
    assert not _vivaldi_version_mismatch(
        "chrome", "Chrome/8.2.4133.68", "Mozilla/5.0 Chrome/152.0.0.0")
    from sweep_lib import check_endpoint_identity
    payload = {"Browser": "Chrome/8.2.4133.68",
               "User-Agent": "Mozilla/5.0 Chrome/152.0.0.0 Safari/537.36"}
    out = check_endpoint_identity("127.0.0.1", 9227, expect_browser="vivaldi",
                                  fetch_version=lambda h, p: dict(payload))
    assert out["Browser"].startswith("Chrome/8")
    # Same payload must still be refused for every branded browser —
    # the mismatch signature must not become a generic bypass. (chrome and
    # chromium are the exceptions: the endpoint self-reports
    # Browser="Chrome/...", so --browser chrome/chromium legitimately
    # match the generic family.)
    for want in ("opera", "edge", "brave", "thorium"):
        with pytest.raises(ValueError):
            check_endpoint_identity(
                "127.0.0.1", 9227, expect_browser=want,
                fetch_version=lambda h, p, _d=payload: dict(_d))


def test_wait_for_endpoint_succeeds_once_vivaldi_http_answers():
    # Regression for the live Vivaldi first-run finding (Windows): the
    # port accepts TCP before /json/version answers, for over a minute.
    # The launch handshake must keep polling through refused/unreachable
    # calls and succeed the moment HTTP starts answering — one early
    # failure must not abort the launch.
    from sweep_lib import wait_for_endpoint
    calls = {"n": 0}

    def startup_window(host, port):
        calls["n"] += 1
        if calls["n"] <= 4:
            raise ConnectionError("WinError 10061 actively refused")
        return {"Browser": "Chrome/8.2.4133.68",
                "User-Agent": "Mozilla/5.0 Chrome/152.0.0.0 Safari/537.36"}

    out = wait_for_endpoint("127.0.0.1", 9227, expect_browser="vivaldi",
                            timeout=10.0, poll_interval=0.05,
                            fetch_version=startup_window)
    assert out["Browser"].startswith("Chrome/8")
    assert calls["n"] == 5  # 4 refused polls, then success on the 5th


def test_wait_for_endpoint_times_out_when_http_never_answers():
    # A startup window that never closes fails closed: ValueError after
    # the timeout — no hang, no false success.
    from sweep_lib import wait_for_endpoint

    def down(host, port):
        raise ConnectionError("refused")

    with pytest.raises(ValueError):
        wait_for_endpoint("127.0.0.1", 9227, expect_browser="vivaldi",
                          timeout=0.4, poll_interval=0.05, fetch_version=down)


def test_windows_process_proof_matches_image_path():
    # Verified live: Thorium on Windows listens from ...\\Thorium\\
    # Application\\thorium.exe while reporting Browser='Chrome/138...'.
    from sweep_lib import verify_endpoint_process_windows

    def list_pids(port):
        assert port == 9222
        return [22292]

    def image_of(pid):
        assert pid == 22292
        return r"C:\Users\sebin\AppData\Local\Thorium\Application\thorium.exe"

    owner = verify_endpoint_process_windows(9222, "thorium",
                                            list_pids=list_pids,
                                            image_of=image_of)
    assert owner == 22292


def test_windows_process_proof_fails_closed():
    from sweep_lib import verify_endpoint_process_windows

    def image_of(_pid):
        return r"C:\other\chrome.exe"

    # non-matching image -> refuse
    with pytest.raises(ValueError):
        verify_endpoint_process_windows(9222, "thorium",
                                        list_pids=lambda p: [1],
                                        image_of=image_of)
    # no listener at all -> refuse
    with pytest.raises(ValueError):
        verify_endpoint_process_windows(9222, "thorium",
                                        list_pids=lambda p: [],
                                        image_of=image_of)
    # empty fragment -> refuse (never a wildcard proof)
    with pytest.raises(ValueError):
        verify_endpoint_process_windows(9222, "  ",
                                        list_pids=lambda p: [1],
                                        image_of=image_of)
    # query infrastructure failure -> refuse, not crash
    def broken(_port):
        raise RuntimeError("powershell missing")

    with pytest.raises(ValueError):
        verify_endpoint_process_windows(9222, "thorium",
                                        list_pids=broken,
                                        image_of=image_of)


def test_confirm_endpoint_vendor_blind_requires_process_proof():
    # Thorium's product string is CDP-identical to Chrome's (verified
    # live). The gate must: refuse without --expect-cmd; accept ONLY
    # product-refusal + passing process proof together; still refuse a
    # generic-family name (chrome) whose product check can never be
    # "vendor blindness".
    from sweep_lib import confirm_endpoint
    tabs = {"A": {"id": "A", "type": "page",
                  "url": "https://ex.com/a", "title": "A"}}

    def proof_ok(port, frag):
        assert frag == "thorium"
        return 22292

    def proof_bad(port, frag):
        raise ValueError("port held by someone else; refusing")

    with FakeCDP("Chrome/138.0.7204.300", tabs) as cdp:
        # no process proof offered -> refuse
        with pytest.raises(ValueError):
            confirm_endpoint("127.0.0.1", cdp.port,
                             expect_browser="thorium")
        # proof offered but failing -> refuse
        with pytest.raises(ValueError):
            confirm_endpoint("127.0.0.1", cdp.port,
                             expect_browser="thorium",
                             expect_cmd="thorium",
                             verify_process=proof_bad)
        # product refused AND proof passes -> allowed, flagged as
        # product_matched=False
        payload, owner, matched = confirm_endpoint(
            "127.0.0.1", cdp.port, expect_browser="thorium",
            expect_cmd="thorium", verify_process=proof_ok)
        assert owner == 22292 and matched is False and payload == {}
        # generic-family name: the strict product path matches the
        # Chrome-shaped product directly, and a provided proof is still
        # mandatory (a failing proof aborts even for chrome).
        payload2, owner2, matched2 = confirm_endpoint(
            "127.0.0.1", cdp.port, expect_browser="chrome",
            expect_cmd="thorium", verify_process=proof_ok)
        assert owner2 == 22292 and matched2 is True
        with pytest.raises(ValueError):
            confirm_endpoint("127.0.0.1", cdp.port,
                             expect_browser="chrome",
                             expect_cmd="thorium",
                             verify_process=proof_bad)


def test_cdp_close_vendor_blind_endpoint_fails_without_process_proof(tmp_path):
    # CLI-level: product-blind --browser with no --expect-cmd aborts
    # before touching tabs, with a pointer to the --expect-cmd remedy.
    tabs = {"A": {"id": "A", "type": "page",
                  "url": "https://ex.com/a", "title": "A"}}
    with FakeCDP("Chrome/138.0.7204.300", tabs) as cdp:
        exp = tmp_path / "expect.json"
        cdp.dump_list(exp)
        ids = tmp_path / "ids.txt"
        ids.write_text("A\n", encoding="utf-8")
        r = run("cdp_close.py", str(ids), "--host", "127.0.0.1",
                "--port", str(cdp.port), "--expect", str(exp),
                "--browser", "thorium")
        assert r.returncode != 0
        assert "--expect-cmd" in (r.stdout + r.stderr)
        assert cdp.closed_puts == []  # nothing was closed


def test_port_inodes_parses_listen_sockets():
    import sweep_lib
    text = ("  sl  local_address rem_address   st tx_queue:rx_queue "
            "tr:tm->when retrnsmt   uid  timeout inode\n"
            "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 "
            "00:00000000 00000000     0        0 12345 1 0000000000000000 100 0 0 10 0\n"
            "   1: 0100007F:0050 00000000:0000 0A 00000000:00000000 "
            "00:00000000 00000000     0        0 999 1 0000000000000000 100 0 0 10 0\n"
            "   2: 0100007F:1F90 0100007F:1F91 01 00000000:00000000 "
            "00:00000000 00000000     0        0 777 1 0000000000000000 100 0 0 10 0\n")
    assert sweep_lib._port_inodes(text, 8080) == {12345}  # 0x1F90; ESTABLISHED ignored


@pytest.mark.skipif(os.name != "posix", reason="needs Linux /proc")
def test_find_pids_sees_real_ipv6_listener():
    # live ::1 socket: proves tcp6 parsing against the real kernel tables,
    # not just the fake-proc fixture above.
    import socket
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("no IPv6 support")
    try:
        try:
            s.bind(("::1", 0))
            s.listen(1)
        except OSError:
            pytest.skip("cannot bind ::1")
        port = s.getsockname()[1]
        try:
            pids = find_pids_listening_on(port)
        except RuntimeError:
            pytest.skip("no /proc net tables readable")
        assert os.getpid() in pids
    finally:
        s.close()


@pytest.mark.skipif(os.name != "posix", reason="fake /proc tree needs POSIX symlinks")
def test_verify_endpoint_process_against_fake_proc(tmp_path):
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue:rx_queue tr:tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 424242 1 0000000000000000 100 0 0 10 0\n",
        encoding="utf-8")
    pid_dir = proc / "4242"
    (pid_dir / "fd").mkdir(parents=True)
    os.symlink("socket:[424242]", pid_dir / "fd" / "3")
    (pid_dir / "cmdline").write_bytes(b"brave\0--user-data-dir=/x/sweeper\0")
    # IPv6 listeners live in net/tcp6 only: second owner via ::1 socket
    (proc / "net" / "tcp6").write_text(
        "  sl  local_address rem_address   st tx_queue:rx_queue tr:tm->when retrnsmt   uid  timeout inode\n"
        "   0: 00000000000000000000000000000001:1F90 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 555555 1 0000000000000000 100 0 0 10 0\n",
        encoding="utf-8")
    pid6_dir = proc / "4343"
    (pid6_dir / "fd").mkdir(parents=True)
    os.symlink("socket:[555555]", pid6_dir / "fd" / "3")
    (pid6_dir / "cmdline").write_bytes(b"brave\0--user-data-dir=/x/sweeper6\0")
    root = str(proc)
    assert find_pids_listening_on(8080, proc_root=root) == [4242, 4343]
    assert "user-data-dir" in read_process_cmdline(4242, proc_root=root)
    assert verify_endpoint_process(8080, "user-data-dir=/x", proc_root=root) == 4242
    with pytest.raises(ValueError):
        verify_endpoint_process(8080, "chrome", proc_root=root)
    with pytest.raises(ValueError):
        verify_endpoint_process(8080, "", proc_root=root)
    assert find_pids_listening_on(9999, proc_root=root) == []
    with pytest.raises(ValueError):
        verify_endpoint_process(9999, "brave", proc_root=root)


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


def test_recount_concurrent_with_appends_loses_nothing(tmp_path):
    # appends and recount-rewrites share one sidecar lock: a rewrite must
    # never clobber an entry appended between its read and replace.
    import threading
    f = tmp_path / "summary.txt"
    f.write_text("# h\n\n0 article tabs summarised.\n\n", encoding="utf-8")
    writers, per_writer = 3, 15

    def appender(k):
        for i in range(per_writer):
            atomic_append(f, [f"## T{k}-{i}\nLink: x\n---\n"])

    def recounter():
        for _ in range(per_writer):
            recount_and_fix_header(f)

    threads = ([threading.Thread(target=appender, args=(k,))
                for k in range(writers)] +
               [threading.Thread(target=recounter) for _ in range(2)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    n = recount_and_fix_header(f)
    assert n == writers * per_writer
    assert f"{n} article tabs summarised." in f.read_text(encoding="utf-8")


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


def test_decode_rejects_non_object_json():
    import lz4.block
    raw = b"mozLz40\0" + lz4.block.compress(b"[1, 2]")
    with pytest.raises(ValueError):
        decode_mozlz4(raw)


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
    assert cleanup_session_copy(dst) is True
    assert not dst.exists()


def test_copy_destinations_unique_per_call(tmp_path):
    src = tmp_path / "sessionstore.jsonlz4"
    src.write_bytes(make_session_bytes())
    a = copy_session_safe(src, tmp_path / "scratch", settle_ms=1)
    b = copy_session_safe(src, tmp_path / "scratch", settle_ms=1)
    assert a != b and a.is_file() and b.is_file()
    assert ".copy." in a.name
    cleanup_session_copy(a)
    cleanup_session_copy(b)


def test_cleanup_reports_refusal_and_failure(tmp_path, monkeypatch):
    import sweep_lib
    # arbitrary path outside temp without marker: refused, kept
    outside = tmp_path / "important.txt"
    outside.write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(
        sweep_lib.tempfile, "gettempdir", lambda: str(tmp_path / "elsewhere"))
    assert sweep_lib.cleanup_session_copy(outside) is False
    assert outside.is_file()
    # missing copy-marked path: nothing to do -> True
    assert sweep_lib.cleanup_session_copy(
        tmp_path / "ghost.copy.jsonlz4") is True


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
    assert info2["newest_backup"] == "recovery.jsonlz4"
    # non-snapshot files (locks, tmp) do not count as fresher state
    (bk / "parent.lock").write_text("x", encoding="utf-8")
    import os
    os.utime(bk / "parent.lock", (info["mtime"] + 50, info["mtime"] + 50))
    info3 = session_freshness(src)
    assert info3["newest_backup"] == "recovery.jsonlz4"


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


def test_close_cli_requires_expect_browser_and_validates(tmp_path):
    ids = tmp_path / "ids.txt"
    ids.write_text("A\n", encoding="utf-8")
    # --expect missing entirely => argparse error, no blind close
    r = run("cdp_close.py", str(ids), "--port", "99999")
    assert r.returncode != 0 and "--expect" in (r.stderr + r.stdout)
    # --browser missing => argparse error, no unattested close
    exp = tmp_path / "exp.json"
    exp.write_text("[]", encoding="utf-8")
    r = run("cdp_close.py", str(ids), "--expect", str(exp))
    assert r.returncode != 0 and "--browser" in (r.stderr + r.stdout)
    # --expect present but bad port still rejected
    r = run("cdp_close.py", str(ids), "--port", "99999",
            "--expect", str(exp), "--browser", "chrome")
    assert r.returncode != 0 and "bad port" in (r.stderr + r.stdout)
    r2 = run("cdp_close.py", str(ids), "--host", "0.0.0.0",
             "--expect", str(exp), "--browser", "chrome")
    assert r2.returncode != 0 and "loopback" in (r2.stderr + r2.stdout)


def test_list_cli_check_endpoint_needs_browser(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps([]), encoding="utf-8")
    r = run("list_cdp_tabs.py", str(good), "--check-endpoint",
            "--host", "127.0.0.1", "--port", "9222")
    assert r.returncode != 0 and "--browser" in (r.stderr + r.stdout)


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


def test_decode_cli_refuses_opencode_substring_live_path(tmp_path):
    # "opencode" as a mere substring is NOT proof of a scratch copy
    tricky = tmp_path / "my-opencode-live-profile" / "sessionstore.jsonlz4"
    tricky.parent.mkdir(parents=True)
    tricky.write_bytes(make_session_bytes())
    r = run("decode_firefox_session.py", str(tricky), "--no-copy")
    assert r.returncode != 0 and "--no-copy" in (r.stderr + r.stdout)


def test_decode_cli_accepts_no_copy_inside_explicit_scratch(tmp_path):
    sc = tmp_path / "scratchdir"
    sc.mkdir()
    plain = sc / "sessionstore.jsonlz4"  # no .copy. marker, but contained
    plain.write_bytes(make_session_bytes())
    r = run("decode_firefox_session.py", str(plain),
            "--no-copy", "--scratch", str(sc))
    assert r.returncode == 0


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


# --- mocked CDP integration (list + close CLI over fake HTTP) ---------------

class FakeCDP:
    """Minimal in-process CDP service: /json/version, /json/list, PUT close."""

    def __init__(self, product, tabs, *, keep_on_close=False,
                 fail_list_after_close=False, also_drop=(),
                 close_delay_lists=0):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.product = product
        self.tabs = dict(tabs)  # id -> {"id","type","url","title"}
        self.keep_on_close = keep_on_close
        self.fail_list = False
        self.fail_list_after_close = fail_list_after_close
        self.also_drop = tuple(also_drop)
        # async-close simulation (Thorium applies close with a delay:
        # 200 OK while still listed for the next N /json/list reads)
        self.close_delay_lists = close_delay_lists
        self.pending_removals = {}  # tab_id -> lists remaining
        self.closed_puts = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                raw = body if isinstance(body, bytes) else body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/json/version":
                    self._send(200, json.dumps(
                        {"Browser": state.product,
                         "Protocol-Version": "1.3"}))
                elif self.path == "/json/list":
                    if state.fail_list:
                        self._send(500, "list down", "text/plain")
                    else:
                        done = [tid for tid, n in
                                state.pending_removals.items() if n <= 1]
                        for tid in done:
                            state.tabs.pop(tid, None)
                            del state.pending_removals[tid]
                        for tid in state.pending_removals:
                            state.pending_removals[tid] -= 1
                        self._send(200, json.dumps(list(state.tabs.values())))
                else:
                    self._send(404, "nope", "text/plain")

            def do_PUT(self):
                import urllib.parse
                if self.path.startswith("/json/close/"):
                    tab_id = urllib.parse.unquote(
                        self.path[len("/json/close/"):])
                    state.closed_puts.append(tab_id)
                    if tab_id in state.tabs and not state.keep_on_close:
                        if state.close_delay_lists > 0:
                            state.pending_removals[tab_id] = \
                                state.close_delay_lists
                        else:
                            del state.tabs[tab_id]
                        for extra in state.also_drop:
                            state.tabs.pop(extra, None)
                    if state.fail_list_after_close:
                        state.fail_list = True
                    self._send(200, "Target closed", "text/plain")
                else:
                    self._send(404, "nope", "text/plain")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05},
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def dump_list(self, path):
        Path(path).write_text(json.dumps(list(self.tabs.values())),
                              encoding="utf-8")


def _closed_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_cdp_list_check_endpoint_happy_and_wrong_browser(tmp_path):
    tabs = {"A": {"id": "A", "type": "page",
                  "url": "https://ex.com/a", "title": "A"}}
    with FakeCDP("Chrome/140.0.0.0", tabs) as cdp:
        dump = tmp_path / "list.json"
        cdp.dump_list(dump)
        r = run("list_cdp_tabs.py", str(dump), "--check-endpoint",
                "--host", "127.0.0.1", "--port", str(cdp.port),
                "--browser", "chrome",
                "--endpoint", f"127.0.0.1:{cdp.port}")
        assert r.returncode == 0 and "A | A | https://ex.com/a" in r.stdout
        # wrong browser: identity mismatch aborts before printing tabs
        r2 = run("list_cdp_tabs.py", str(dump), "--check-endpoint",
                 "--host", "127.0.0.1", "--port", str(cdp.port),
                 "--browser", "brave")
        assert r2.returncode != 0 and "reports Browser" in (
            r2.stderr + r2.stdout)
        assert "https://ex.com/a" not in r2.stdout


def test_cdp_close_happy_path(tmp_path):
    tabs = {"A": {"id": "A", "type": "page",
                  "url": "https://ex.com/a", "title": "A"}}
    with FakeCDP("Chrome/140.0.0.0", tabs) as cdp:
        exp = tmp_path / "expect.json"
        cdp.dump_list(exp)
        ids = tmp_path / "ids.txt"
        ids.write_text("A\n", encoding="utf-8")
        r = run("cdp_close.py", str(ids), "--host", "127.0.0.1",
                "--port", str(cdp.port), "--expect", str(exp),
                "--browser", "chrome",
                "--endpoint", f"127.0.0.1:{cdp.port}")
        assert r.returncode == 0, r.stderr + r.stdout
        assert "closed ok: 1/1" in r.stderr
        assert cdp.tabs == {} and cdp.closed_puts == ["A"]


def test_cdp_close_skips_navigated_tab(tmp_path):
    live = {"A": {"id": "A", "type": "page",
                  "url": "https://ex.com/OTHER", "title": "A"}}
    with FakeCDP("Chrome/140.0.0.0", live) as cdp:
        exp = tmp_path / "expect.json"
        exp.write_text(json.dumps([
            {"id": "A", "type": "page",
             "url": "https://ex.com/a", "title": "A"}]), encoding="utf-8")
        ids = tmp_path / "ids.txt"
        ids.write_text("A\n", encoding="utf-8")
        r = run("cdp_close.py", str(ids), "--host", "127.0.0.1",
                "--port", str(cdp.port), "--expect", str(exp),
                "--browser", "chrome")
        assert r.returncode == 0, r.stderr + r.stdout
        assert "navigated since approval" in (r.stdout + r.stderr)
        assert "A" in cdp.tabs and cdp.closed_puts == []


def test_cdp_close_fails_when_target_stays_listed(tmp_path):
    tabs = {"A": {"id": "A", "type": "page",
                  "url": "https://ex.com/a", "title": "A"}}
    with FakeCDP("Chrome/140.0.0.0", tabs, keep_on_close=True) as cdp:
        exp = tmp_path / "expect.json"
        cdp.dump_list(exp)
        ids = tmp_path / "ids.txt"
        ids.write_text("A\n", encoding="utf-8")
        r = run("cdp_close.py", str(ids), "--host", "127.0.0.1",
                "--port", str(cdp.port), "--expect", str(exp),
                "--browser", "chrome")
        assert r.returncode != 0
        assert "still listed" in (r.stdout + r.stderr)


def test_cdp_close_tolerates_async_close(tmp_path):
    # real finding: Thorium answers 200 while still listing the target;
    # disappearance follows a beat later. The CLI polls instead of
    # failing on the first still-listed read.
    tabs = {"A": {"id": "A", "type": "page",
                  "url": "https://ex.com/a", "title": "A"}}
    with FakeCDP("Chrome/140.0.0.0", tabs, close_delay_lists=2) as cdp:
        exp = tmp_path / "expect.json"
        cdp.dump_list(exp)
        ids = tmp_path / "ids.txt"
        ids.write_text("A\n", encoding="utf-8")
        r = run("cdp_close.py", str(ids), "--host", "127.0.0.1",
                "--port", str(cdp.port), "--expect", str(exp),
                "--browser", "chrome")
        assert r.returncode == 0, r.stderr + r.stdout
        assert "closed ok: 1/1" in r.stderr


def test_cdp_close_fails_when_post_close_list_unreachable(tmp_path):
    tabs = {"A": {"id": "A", "type": "page",
                  "url": "https://ex.com/a", "title": "A"}}
    with FakeCDP("Chrome/140.0.0.0", tabs,
                 fail_list_after_close=True) as cdp:
        exp = tmp_path / "expect.json"
        cdp.dump_list(exp)
        ids = tmp_path / "ids.txt"
        ids.write_text("A\n", encoding="utf-8")
        r = run("cdp_close.py", str(ids), "--host", "127.0.0.1",
                "--port", str(cdp.port), "--expect", str(exp),
                "--browser", "chrome")
        assert r.returncode != 0
        assert "UNVERIFIED" in (r.stdout + r.stderr)


def test_cdp_close_flags_unexpected_disappearance(tmp_path):
    tabs = {"A": {"id": "A", "type": "page",
                  "url": "https://ex.com/a", "title": "A"},
            "C": {"id": "C", "type": "page",
                  "url": "https://ex.com/c", "title": "C"}}
    with FakeCDP("Chrome/140.0.0.0", tabs, also_drop=("C",)) as cdp:
        exp = tmp_path / "expect.json"
        cdp.dump_list(exp)
        ids = tmp_path / "ids.txt"
        ids.write_text("A\n", encoding="utf-8")
        r = run("cdp_close.py", str(ids), "--host", "127.0.0.1",
                "--port", str(cdp.port), "--expect", str(exp),
                "--browser", "chrome")
        assert r.returncode != 0
        assert "unexpectedly closed" in (r.stderr + r.stdout)


def test_cdp_close_wrong_browser_and_dead_port(tmp_path):
    tabs = {"A": {"id": "A", "type": "page",
                  "url": "https://ex.com/a", "title": "A"}}
    with FakeCDP("Chrome/140.0.0.0", tabs) as cdp:
        exp = tmp_path / "expect.json"
        cdp.dump_list(exp)
        ids = tmp_path / "ids.txt"
        ids.write_text("A\n", encoding="utf-8")
        base = [str(ids), "--host", "127.0.0.1", "--port", str(cdp.port),
                "--expect", str(exp)]
        r = run("cdp_close.py", *base, "--browser", "brave")
        assert r.returncode != 0 and "reports Browser" in (
            r.stderr + r.stdout)
        assert cdp.closed_puts == []  # no close attempted
    dead = _closed_port()
    r2 = run("cdp_close.py", str(ids), "--host", "127.0.0.1",
             "--port", str(dead), "--expect", str(exp),
             "--browser", "chrome")
    assert r2.returncode != 0 and "unreachable" in (r2.stderr + r2.stdout)


def test_cdp_close_expect_cmd_mismatch_refuses(tmp_path):
    tabs = {"A": {"id": "A", "type": "page",
                  "url": "https://ex.com/a", "title": "A"}}
    with FakeCDP("Chrome/140.0.0.0", tabs) as cdp:
        exp = tmp_path / "expect.json"
        cdp.dump_list(exp)
        ids = tmp_path / "ids.txt"
        ids.write_text("A\n", encoding="utf-8")
        r = run("cdp_close.py", str(ids), "--host", "127.0.0.1",
                "--port", str(cdp.port), "--expect", str(exp),
                "--browser", "chrome",
                "--expect-cmd", "definitely-not-this-process-xyz")
        assert r.returncode != 0 and "refusing" in (
            r.stderr + r.stdout)  # fail-closed message shared by
        # /proc cmdline (Linux CI) and image-path (Windows) proofs
        assert cdp.closed_puts == []  # refused before any close
