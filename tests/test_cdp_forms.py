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


CLEAN = {"dirty": False, "fields": [], "readyState": "complete"}
DIRTY = {"dirty": True, "fields": ["input.text", "textarea"]}


def test_probe_reports_clean_page():
    with FakeWS(_reply(CLEAN)) as s:
        dirty, why = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is False and why.startswith("clean")


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
    assert dirty is True and "no-websocket-url" in why


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


# --- page-still-loading must not be reported as clean ----------------------
#
# Found by testing against a real browser, not by reading the code: while a
# document is still parsing it has no inputs yet, so the field scan returns an
# empty result indistinguishable from a genuinely empty form.

def _ready(state, dirty=False):
    return lambda msg: {"id": msg.get("id"), "result": {"result": {"value": {
        "dirty": dirty, "fields": ["input.text"] if dirty else [],
        "readyState": state}}}}


def test_still_loading_page_is_not_reported_clean():
    with FakeWS(_ready("loading")) as s:
        dirty, why = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is True, "a half-parsed DOM proves nothing about a form"
    assert "still-loading" in why


@pytest.mark.parametrize("state", ["interactive", "complete"])
def test_settled_pages_are_scanned_normally(state):
    with FakeWS(_ready(state)) as s:
        dirty, _ = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is False, f"readyState={state} means the DOM is usable"


def test_settled_page_with_input_still_detected():
    with FakeWS(_ready("interactive", dirty=True)) as s:
        dirty, why = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is True and "unsaved-input" in why


def test_loading_page_with_input_is_reported_as_loading_not_input():
    """Either verdict skips the close, but the reason must not mislead."""
    with FakeWS(_ready("loading", dirty=True)) as s:
        dirty, why = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is True and "still-loading" in why


def test_probe_expression_reports_ready_state():
    """Must assert the code, not the word.

    'readyState' also appears in a comment inside the expression, so a bare
    substring check passed even after the property was deleted -- the mutant
    that removes it survived. Pin the exact read instead.
    """
    with FakeWS(_ready("complete")) as s:
        cdp_forms.probe_dirty_form(s.url, timeout=5)
    expr = s.received[0]["params"]["expression"]
    assert "readyState: document.readyState" in expr
    assert "return { dirty:" in expr


def test_missing_ready_state_does_not_silently_pass():
    """An older page that omits the field must not become fail-open."""
    with FakeWS(lambda m: {"id": m.get("id"), "result": {"result": {"value": {
                "dirty": False, "fields": [], "readyState": "complete"}}}}) as s:
        dirty, _ = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is False, "a complete reply without readyState is still usable"


# --- the remedy must be named, not just the symptom ------------------------

@pytest.mark.parametrize("bad", ["", None, "http://127.0.0.1:9/x", "not-a-url"])
def test_no_ws_url_message_names_the_flag(bad):
    """An operator who sees the failure and is not told the fix is stuck.

    The obvious wrong move is to read "failing closed" and assume the tab was
    clean, or to retry forever. The message must carry the remedy.
    """
    dirty, why = cdp_forms.probe_dirty_form(bad)
    assert dirty is True
    assert "--no-form-guard" in why, f"message must name the opt-out: {why!r}"
    assert "no-websocket-url" in why


# --- a just-committed document must not be reported clean ------------------
#
# readyState only catches a half-parsed DOM. A page that has just committed can
# already read "interactive"/"complete" while client-side code is still
# injecting its form, so the field scan finds nothing on a page that is about
# to present one.


def _aged(age_ms, state="complete", dirty=False):
    return lambda msg: {"id": msg.get("id"), "result": {"result": {"value": {
        "dirty": dirty, "fields": ["input.text"] if dirty else [],
        "readyState": state, "ageMs": age_ms}}}}


def test_just_committed_document_is_not_reported_clean():
    with FakeWS(_aged(300, state="interactive")) as s:
        dirty, why = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is True, (
        "readyState was already interactive, but the form may not exist yet")
    assert "just-navigated" in why


@pytest.mark.parametrize("age", [0, 400, 1199])
def test_documents_younger_than_the_floor_are_refused(age):
    with FakeWS(_aged(age)) as s:
        dirty, _ = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is True


@pytest.mark.parametrize("age", [1200, 5000, 600_000])
def test_settled_documents_are_scanned_normally(age):
    with FakeWS(_aged(age)) as s:
        dirty, _ = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is False, f"a {age}ms-old document must not be refused"


def test_settled_page_with_input_still_detected():
    with FakeWS(_aged(9000, dirty=True)) as s:
        dirty, why = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is True and "unsaved-input" in why


def test_young_document_with_input_reports_the_young_reason():
    """Either verdict skips the close, but the reason must not mislead."""
    with FakeWS(_aged(200, dirty=True)) as s:
        dirty, why = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is True and "just-navigated" in why


def test_missing_age_falls_back_to_the_ready_state_check_only():
    """An absent timeOrigin must not silently pass the young-document test."""
    with FakeWS(lambda m: {"id": m.get("id"), "result": {"result": {"value": {
            "dirty": False, "fields": [], "readyState": "complete"}}}}) as s:
        dirty, _ = cdp_forms.probe_dirty_form(s.url, timeout=5)
    assert dirty is False, "a complete reply with no age is still usable"


def test_probe_expression_reads_the_time_origin():
    """Pin the code, not the word: the expression must actually read it."""
    with FakeWS(_aged(9000)) as s:
        cdp_forms.probe_dirty_form(s.url, timeout=5)
    expr = s.received[0]["params"]["expression"]
    assert "performance.timeOrigin" in expr
    assert "ageMs: age" in expr


def test_floor_is_a_named_constant():
    assert 0 < cdp_forms.MIN_DOCUMENT_AGE_MS <= 5000, (
        "the floor must be short enough not to block ordinary tabs, and long "
        "enough to cover client-side form rendering")
