"""Dirty-form guard: WebSocket client + probe, against a real fake server."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_ws import FakeWS  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / "article-sweeper" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cdp_forms  # noqa: E402


def _reply(value):
    return lambda msg: {"id": msg.get("id"), "result": {"result": {"value": value}}}


CLEAN = {"dirty": False, "fields": []}
DIRTY = {"dirty": True, "fields": ["input.text", "textarea"]}


def test_probe_reports_clean_page():
    with FakeWS(_reply(CLEAN)) as s:
        dirty, why = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is False and why == "clean"


def test_probe_reports_unsaved_input():
    with FakeWS(_reply(DIRTY)) as s:
        dirty, why = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is True
    assert "unsaved-input" in why and "input.text" in why


def test_probe_sends_runtime_evaluate():
    with FakeWS(_reply(CLEAN)) as s:
        cdp_forms.probe_dirty_form(s.url, timeout=5)
        msg = s.received[0]
    assert msg["method"] == "Runtime.evaluate"
    assert msg["params"]["returnByValue"] is True
    assert "contenteditable" in msg["params"]["expression"]


@pytest.mark.parametrize("kwargs,frag", [
    ({"reject_handshake": True}, "probe-failed"),
    ({"bad_accept": True}, "probe-failed"),
    ({"drop_after_handshake": True}, "probe-failed"),
    ({"never_reply": True}, "probe-failed"),
])
def test_probe_fails_closed_on_transport_failure(kwargs, frag):
    """A guard that passes when it cannot see is worse than no guard."""
    with FakeWS(_reply(CLEAN), **kwargs) as s:
        dirty, why = cdp_forms.probe_dirty_form(s.url, timeout=3)
    assert dirty is True, "unverifiable must mean do-not-close"
    assert frag in why


def test_probe_fails_closed_on_unparseable_result():
    with FakeWS(_reply("not-an-object")) as s:
        dirty, why = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is True and "unparseable" in why


def test_probe_fails_closed_on_cdp_error():
    with FakeWS(lambda m: {"id": m.get("id"),
                          "error": {"code": -32000, "message": "detached"}}) as s:
        dirty, why = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is True and "CDP error" in why


@pytest.mark.parametrize("url", ["", None, "http://127.0.0.1:9/x", "not-a-url"])
def test_probe_without_websocket_url_fails_closed(url):
    dirty, why = cdp_forms.probe_dirty_form(url)
    assert dirty is True and "failing closed" in why


def test_probe_never_raises():
    """Every failure mode must come back as a verdict, not an exception."""
    for url in ("", "ws://127.0.0.1:1/x", "ws://256.256.256.256/x", 12345):
        out = cdp_forms.probe_dirty_form(url, timeout=1)
        assert isinstance(out, tuple) and out[0] is True


def test_large_payload_round_trips():
    """Extended length encoding must survive a reply bigger than 125 bytes."""
    big = {"dirty": False, "fields": ["x" * 400 for _ in range(3)]}
    with FakeWS(_reply(big)) as s:
        dirty, _ = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is False
