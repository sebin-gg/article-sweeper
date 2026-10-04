"""Stability window: enumerate only once the tab set stops changing."""
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "article-sweeper" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sweep_lib import (  # noqa: E402
    STABLE_MIN_TABS,
    STABLE_SETTLE_SECONDS,
    wait_for_stable_tabs,
)


def _tabs(*ids):
    return [{"id": i, "type": "page", "url": f"https://ex.com/{i}",
             "title": i} for i in ids]


class Clock:
    """Virtual time. Makes settle windows deterministic instead of flaky."""

    def __init__(self):
        self.now = 0.0
        self.slept = 0.0

    def sleep(self, s):
        self.now += s
        self.slept += s

    def mono(self):
        return self.now


def _drive(script, *, cycle=False, **kw):
    """script: raw payloads, one per fetch call.

    `cycle=False` clamps to the last element (settles). `cycle=True` repeats
    forever, for a session that genuinely never stops changing.
    """
    clock = Clock()
    calls = []

    def fetch():
        i = len(calls)
        calls.append(i)
        idx = i % len(script) if cycle else min(i, len(script) - 1)
        return script[idx]

    import sweep_lib
    real = sweep_lib.time.monotonic
    sweep_lib.time.monotonic = clock.mono
    try:
        recs, info = wait_for_stable_tabs(fetch, sleep=clock.sleep, **kw)
    finally:
        sweep_lib.time.monotonic = real
    return recs, info, calls


# --- it waits when tabs are still appearing --------------------------------

def test_waits_while_session_restore_is_adding_tabs():
    growing = [_tabs("A"), _tabs("A", "B"), _tabs("A", "B", "C"),
               _tabs("A", "B", "C")]
    recs, info, calls = _drive(growing, settle_seconds=2.0, poll=0.25)
    assert info["stable"] is True
    assert len(recs) == 3, "must not enumerate the partial 1-tab or 2-tab view"
    assert info["changes"] == 2, info
    assert calls[-1] >= 3


def test_immediate_stability_returns_after_one_settle_window():
    big = _tabs(*[f"T{i}" for i in range(STABLE_MIN_TABS + 1)])
    recs, info, calls = _drive([big], settle_seconds=2.0, poll=0.25)
    assert info["stable"] is True and len(recs) == len(big)
    assert info["changes"] == 0
    assert info["waited"] >= 2.0, "must actually hold the window"


def test_small_pile_uses_the_shorter_window():
    """Do not pay two seconds to learn there are four tabs."""
    recs, info, _ = _drive([_tabs("A", "B", "C", "D")], poll=0.25,
                           settle_seconds=2.0, settle_small=0.5)
    assert info["stable"] is True
    assert info["settle"] == 0.5
    assert info["waited"] >= 0.5, "the short window must still be honoured"
    assert info["waited"] < 2.0


def test_large_pile_uses_the_full_window():
    big = _tabs(*[f"T{i}" for i in range(STABLE_MIN_TABS + 2)])
    recs, info, _ = _drive([big], poll=0.25, settle_seconds=2.0, settle_small=0.5)
    assert info["settle"] == 2.0
    assert info["waited"] >= 2.0


# --- count alone is not stability ------------------------------------------

def test_equal_count_different_set_is_not_stable():
    """One tab closes while another opens: the count never moves."""
    flipping = [_tabs("A", "B"), _tabs("A", "C"), _tabs("A", "B"),
                _tabs("A", "C"), _tabs("A", "B")]
    recs, info, _ = _drive(flipping, settle_seconds=1.0, poll=0.25)
    assert info["stable"] is True
    assert info["changes"] >= 3, (
        "a constant count with a changing id set must keep resetting")


def test_signature_covers_ids_not_just_count():
    from sweep_lib import tab_signature
    a = tab_signature(_tabs("A", "B"))
    b = tab_signature(_tabs("A", "C"))
    assert a[0] == b[0], "counts match by construction"
    assert a[1] != b[1], "but the id sets differ, so it is not stable"


def test_signature_tolerates_malformed_entries():
    from sweep_lib import tab_signature
    sig = tab_signature([{"id": "A"}, {"id": ""}, {"nope": 1}, "junk", None])
    assert sig == (5, frozenset({"A"}))


# --- it must not hang ------------------------------------------------------

def test_timeout_on_a_never_stabilising_session():
    _, info, calls = _drive([_tabs("A"), _tabs("B"), _tabs("C")], cycle=True,
                            settle_seconds=5.0, settle_small=5.0,
                            poll=0.25, timeout=3.0)
    assert info["stable"] is False
    assert info["timed_out"] is True
    assert info["waited"] >= 3.0
    assert len(calls) < 100, "timeout must actually bound the polling"


def test_timeout_returns_the_last_snapshot_not_nothing():
    recs, info, _ = _drive([_tabs("A", "B")], settle_seconds=99.0,
                           settle_small=99.0, poll=0.25, timeout=1.0)
    assert info["stable"] is False
    assert len(recs) == 2, "a moving target is not a reason to abort the sweep"


def test_fetch_failures_do_not_abort_the_run():
    """A blip must not cost the caller its snapshot."""
    clock = Clock()
    seq = [OSError("connection refused"), _tabs("A"), _tabs("A"), _tabs("A")]
    calls = []

    def fetch():
        i = len(calls)
        calls.append(i)
        item = seq[min(i, len(seq) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    import sweep_lib
    real = sweep_lib.time.monotonic
    sweep_lib.time.monotonic = clock.mono
    try:
        recs, info = wait_for_stable_tabs(fetch, sleep=clock.sleep,
                                          settle_small=0.5, poll=0.25)
    finally:
        sweep_lib.time.monotonic = real
    assert len(recs) == 1 and info["stable"] is True
    assert calls[0] == 0, "the first fetch was the failing one"


def test_total_fetch_failure_returns_empty_and_says_so():
    def boom():
        raise OSError("down")

    import sweep_lib
    recs, info = wait_for_stable_tabs(boom, timeout=0.5, poll=0.1,
                                      sleep=lambda s: None)
    assert recs == []
    assert info["stable"] is False and info["timed_out"] is True


def test_persistent_malformed_payload_raises_from_parse():
    from sweep_lib import parse_cdp_list
    with pytest.raises(ValueError):
        parse_cdp_list("not a list")
