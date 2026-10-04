"""Companion index.json: machine-readable provenance for every entry."""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "article-sweeper" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sweep_lib import SummaryStream, parse_summary_file  # noqa: E402


def _entry(title, link):
    return [f"## {title}\n", f"Link: {link}\n", "Summary: body.\n",
            "Takeaway: point.\n", "---\n", "\n"]


def _run(tmp_path, specs):
    s = SummaryStream(tmp_path / "summary.md", expected=len(specs),
                      index_path=tmp_path / "index.json")
    for kw in specs:
        s.emit(_entry(kw["title"], kw["link"]), **kw)
    manifest = s.finalize()
    return s, manifest, json.loads((tmp_path / "index.json").read_text())


def test_index_records_provenance_fields(tmp_path):
    _, _, doc = _run(tmp_path, [{
        "title": "Deep Dive", "link": "https://news.example/x",
        "url": "https://news.example/x", "tab_ids": ["T7", "T3"],
        "source": "fetch", "tab_index": 3,
    }])
    e = doc["entries"][0]
    for field in ("title", "canonical", "link", "date", "tab_ids", "source",
                  "domain"):
        assert field in e, f"missing {field}"
    assert e["tab_ids"] == ["T3", "T7"], "tab ids should be sorted, not raw"
    assert e["domain"] == "news.example"
    assert e["source"] == "fetch"


def test_index_records_search_sources(tmp_path):
    """An unverified summary must remain distinguishable months later."""
    _, _, doc = _run(tmp_path, [{
        "title": "Search Only", "link": "https://paywalled.example/y",
        "url": "https://paywalled.example/y", "source": "search",
        "sources_consulted": ["https://result.example/2", "https://result.example/1"],
        "tab_ids": ["T1"], "tab_index": 1,
    }])
    e = doc["entries"][0]
    assert e["source"] == "search"
    assert e["sources_consulted"] == ["https://result.example/1",
                                      "https://result.example/2"]
    assert e["date"], "entries must be dated"


def test_index_order_is_deterministic_not_completion_order(tmp_path):
    specs = [
        {"title": "Zed", "link": "https://zeta.example/1", "tab_index": 3},
        {"title": "Abe", "link": "https://alpha.example/1", "tab_index": 2},
        {"title": "Mid", "link": "https://alpha.example/2", "tab_index": 1},
    ]
    _, _, doc = _run(tmp_path, specs)
    order = [e["link"] for e in doc["entries"]]
    assert order == ["https://alpha.example/2",   # alpha, index 1
                     "https://alpha.example/1",   # alpha, index 2
                     "https://zeta.example/1"]    # zeta,  index 3


def test_index_order_is_stable_across_runs(tmp_path):
    """Same pile in a different completion order -> byte-identical index."""
    a = [{"title": "One", "link": "https://a.example/1", "tab_index": 1},
         {"title": "Two", "link": "https://b.example/2", "tab_index": 2}]
    b = list(reversed(a))
    _run(tmp_path / "r1", a)
    _run(tmp_path / "r2", b)
    p1 = (tmp_path / "r1" / "index.json").read_text()
    p2 = (tmp_path / "r2" / "index.json").read_text()
    assert _without_timestamps(p1) == _without_timestamps(p2), (
        "index ordering must not depend on completion order")


def _without_timestamps(text):
    doc = json.loads(text)
    doc.pop("generated", None)
    doc.pop("run_date", None)
    return json.dumps(doc, sort_keys=True)


def test_index_matches_the_prose_file_exactly(tmp_path):
    """The two artifacts must never disagree about what was summarized."""
    specs = [{"title": "One", "link": "https://a.example/1", "tab_index": 1},
             {"title": "Two", "link": "https://b.example/2", "tab_index": 2}]
    _, _, doc = _run(tmp_path, specs)
    parsed, problems = parse_summary_file(tmp_path / "summary.md")
    assert not problems, problems
    assert {e["canonical"] for e in doc["entries"]} == set(parsed), (
        "index.json and summary.md disagree about which articles were filed")


def test_index_omits_entries_that_fail_schema(tmp_path):
    """A malformed entry is not trusted as a record either."""
    s = SummaryStream(tmp_path / "summary.md", expected=1,
                      index_path=tmp_path / "index.json")
    s.emit(["## Broken\n", "Summary: no link here.\n", "---\n"],
           url="https://x.example/1")
    manifest = s.finalize()
    doc = json.loads((tmp_path / "index.json").read_text())
    assert doc["entries"] == [], "invalid entries must not enter the index"
    assert manifest["complete"] is False


def test_index_is_absent_when_not_requested(tmp_path):
    s = SummaryStream(tmp_path / "summary.md", expected=1)
    s.emit(_entry("One", "https://a.example/1"))
    manifest = s.finalize()
    assert "index" not in manifest
    assert not (tmp_path / "index.json").exists()


def test_index_reflects_a_partial_run(tmp_path):
    s = SummaryStream(tmp_path / "summary.md", expected=5,
                      index_path=tmp_path / "index.json")
    s.emit(_entry("One", "https://a.example/1"), tab_index=1)
    manifest = s.finalize()
    doc = json.loads((tmp_path / "index.json").read_text())
    assert doc["complete"] is False and doc["expected"] == 5
    assert doc["emitted"] == 1 and len(doc["entries"]) == 1


def test_index_is_valid_json_and_sorted_keys(tmp_path):
    _, _, doc = _run(tmp_path, [{"title": "One", "link": "https://a.example/1"}])
    raw = (tmp_path / "index.json").read_text()
    assert json.loads(raw) == doc
    assert '"canonical"' in raw and raw.index('"canonical"') < raw.index('"domain"')


def test_emit_without_metadata_still_works(tmp_path):
    """Backwards compatibility: the old emit(entry_lines, url=...) signature."""
    s = SummaryStream(tmp_path / "summary.md", expected=1,
                      index_path=tmp_path / "index.json")
    s.emit(_entry("Legacy", "https://a.example/1"), url="https://a.example/1")
    s.finalize()          # the index is written on finalize, not on emit
    doc = json.loads((tmp_path / "index.json").read_text())
    assert doc["entries"][0]["title"] == "Legacy"
    assert doc["entries"][0]["tab_ids"] == []
