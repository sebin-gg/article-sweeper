"""Token-sized batching: bound by estimated context, not by article count."""
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "article-sweeper" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sweep_lib import (  # noqa: E402
    BATCH_OVERHEAD_TOKENS,
    estimate_tokens,
    plan_token_batches,
)


def _item(chars, tag=""):
    return {"text": "w" * chars, "tag": tag}


def _tok(it):
    return estimate_tokens(it["text"])


# --- the estimate ----------------------------------------------------------

def test_estimate_is_about_four_chars_per_token():
    assert estimate_tokens("w" * 4000) == 1000
    assert estimate_tokens("") == 0
    assert estimate_tokens("x") == 1, "never zero for non-empty"


def test_estimate_is_monotonic():
    assert estimate_tokens("w" * 8000) > estimate_tokens("w" * 4000)


# --- the budget is respected ----------------------------------------------

def test_batches_respect_the_token_budget():
    items = [_item(4000, f"m{i}") for i in range(12)]   # ~1000 tokens each
    batches, info = plan_token_batches(items, token_budget=5000, overhead=0)
    for b in batches:
        assert sum(_tok(i) for i in b) <= 5000
    assert all(b for b in batches), "no empty batches"


def test_overhead_is_subtracted_from_the_budget():
    items = [_item(4000) for _ in range(6)]
    _, info = plan_token_batches(items, token_budget=6000,
                                 overhead=BATCH_OVERHEAD_TOKENS)
    assert info["per_batch_budget"] == 6000 - BATCH_OVERHEAD_TOKENS
    assert info["budget"] == 6000


def test_max_items_caps_a_batch_of_tiny_articles():
    """Otherwise a pile of stubs collapses into one enormous batch."""
    items = [_item(4) for _ in range(40)]     # 1 token each
    batches, info = plan_token_batches(items, token_budget=10_000_000,
                                       max_items=12, overhead=0)
    assert info["max_batch_items"] == 12
    assert info["batches"] == 4, "40 tiny items at 12/batch"


def test_no_items_are_lost_or_duplicated():
    items = [_item(3000, str(i)) for i in range(23)]
    batches, info = plan_token_batches(items, token_budget=4000, overhead=0)
    flat = [i["tag"] for b in batches for i in b]
    assert sorted(flat) == sorted(i["tag"] for i in items)
    assert len(flat) == len(set(flat)) == len(items)


# --- determinism -----------------------------------------------------------

def test_batching_is_deterministic_for_the_same_input():
    items = [_item(3000 + (i % 5) * 700, str(i)) for i in range(20)]
    a, ia = plan_token_batches(items, token_budget=9000, overhead=0)
    b, ib = plan_token_batches(items, token_budget=9000, overhead=0)
    assert [[i["tag"] for i in batch] for batch in a] == \
           [[i["tag"] for i in batch] for batch in b]
    assert ia == ib


def test_input_order_is_preserved():
    items = [_item(2000, str(i)) for i in range(10)]
    batches, _ = plan_token_batches(items, token_budget=6000, overhead=0)
    flat = [i["tag"] for b in batches for i in b]
    assert flat == [str(i) for i in range(10)], (
        "reordering breaks diffing against the previous run")


# --- oversized items -------------------------------------------------------

def test_oversized_item_gets_its_own_batch_and_is_reported():
    big = _item(400_000, "huge")
    items = [_item(2000, "a"), big, _item(2000, "b")]
    batches, info = plan_token_batches(items, token_budget=5000, overhead=0)
    assert big in info["oversized"], "a straggler must be visible, not hidden"
    assert any(b == [big] for b in batches), "it must not share a batch"


def test_oversized_is_never_truncated():
    """Silently capping it would hand the summarizer a partial article."""
    big = _item(400_000, "huge")
    batches, info = plan_token_batches([big], token_budget=1000, overhead=0)
    assert batches[0][0]["text"] == big["text"]
    assert len(batches[0][0]["text"]) == 400_000


def test_oversized_flushes_the_current_batch_first():
    items = [_item(2000, "a"), _item(2000, "b"), _item(400_000, "big")]
    batches, _ = plan_token_batches(items, token_budget=5000, overhead=0)
    assert batches[-1] == [items[2]], "the oversized item ends its own batch"
    assert items[0] not in batches[-1]


# --- edges -----------------------------------------------------------------

def test_empty_input_yields_no_batches():
    batches, info = plan_token_batches([], token_budget=1000)
    assert batches == [] and info["batches"] == 0


def test_degenerate_budgets_do_not_hang():
    items = [_item(4000) for _ in range(5)]
    for budget in (0, 1, -5):
        batches, _ = plan_token_batches(items, token_budget=budget, overhead=0)
        assert sum(len(b) for b in batches) == len(items)


def test_zero_max_items_does_not_divide_by_zero():
    batches, _ = plan_token_batches([_item(10)], token_budget=1000,
                                    max_items=0, overhead=0)
    assert sum(len(b) for b in batches) == 1


def test_plain_objects_with_text_work():
    class A:
        def __init__(self, t):
            self.text = t
    batches, info = plan_token_batches([A("w" * 40_000)], token_budget=3000,
                                       overhead=0)
    assert info["oversized"], "attribute access must work, not just dicts"


# --- the problem this exists to solve --------------------------------------

def test_count_batching_would_straggle_and_token_batching_would_not():
    """5 Medium posts + 3 long pieces, budget sized for one batch."""
    items = ([_item(6000, f"med{i}") for i in range(5)] +
             [_item(30000, f"long{i}") for i in range(3)])
    budget, overhead = 46_000, 0

    by_count = [items[i:i + 4] for i in range(0, len(items), 4)]
    spread = (max(sum(_tok(i) for i in b) for b in by_count) -
              min(sum(_tok(i) for i in b) for b in by_count))

    tok_batches, info = plan_token_batches(items, token_budget=budget,
                                           overhead=overhead)
    tok_spread = (max(sum(_tok(i) for i in b) for b in tok_batches) -
                  min(sum(_tok(i) for i in b) for b in tok_batches))
    assert spread > 15_000, "the count-based baseline must show the straggler"
    assert tok_spread == 0, "token sizing must equalise the batches"


def test_multiple_token_batches_are_balanced():
    """Wall-clock is bounded by the SLOWEST batch.

    With a budget that forces several batches, count-based batching gives one
    light batch and one heavy one -- the light one's agents finish early and
    sit idle holding context while the heavy one finishes. Token sizing
    equalises them.
    """
    items = ([_item(6000, f"m{i}") for i in range(5)] +
             [_item(30000, f"b{i}") for i in range(3)])
    budget = 20_000   # total is 30_000, so this forces two batches

    by_count = [items[i:i + 4] for i in range(0, len(items), 4)]
    count_loads = [sum(_tok(i) for i in b) for b in by_count]
    tok_batches, _ = plan_token_batches(items, token_budget=budget, overhead=0)
    tok_loads = [sum(_tok(i) for i in b) for b in tok_batches]

    assert len(tok_batches) > 1, "the budget must force several batches"
    assert (max(count_loads) - min(count_loads)) > 10_000
    assert (max(tok_loads) - min(tok_loads)) < 2_000, (
        f"token batches should be near-equal, got {tok_loads}")
