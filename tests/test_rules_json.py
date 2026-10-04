"""rules.json: single source of truth, schema-validated, behaviour-preserving."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "article-sweeper" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sweep_lib  # noqa: E402
from sweep_lib import RulesError, classify_url, load_rules  # noqa: E402


# --- the file exists, is valid, and is what the code is using --------------

def test_rules_file_ships_with_the_skill():
    assert sweep_lib.RULES_PATH.exists(), "rules.json must ship with the skill"
    assert sweep_lib.RULES_PATH.parent == ROOT / "article-sweeper"


def test_rules_load_and_validate():
    rules = load_rules()
    assert rules["version"] == 1


def test_every_constant_is_derived_from_the_file():
    """No rule may live only in Python.

    This is the whole point: if a constant is defined as a literal somewhere in
    sweep_lib it will drift from rules.json and the file stops being the truth.
    """
    r = sweep_lib.RULES
    assert sweep_lib.TRACKING_PARAMS == frozenset(r["tracking_params"])
    assert sweep_lib.SENSITIVE_PARAMS == frozenset(r["sensitive_params"])
    assert sweep_lib.WRAPPER_DOMAINS == tuple(r["wrapper_domains"])
    assert sweep_lib.BLOCKED_HOST_SUBSTRINGS == tuple(r["blocked_hosts"]["substrings"])
    assert sweep_lib.INTERNAL_SCHEMES == tuple(r["internal_schemes"])
    assert sweep_lib.NON_ARTICLE_SEGMENTS == frozenset(r["non_article"]["segments"])
    assert sweep_lib.NEVER_CLOSE_SEGMENTS == frozenset(r["never_close"]["segments"])
    assert sweep_lib.PAYWALLED_HOSTS == frozenset(r["paywalled_hosts"])
    assert sweep_lib.BLOCKED_STATUS == frozenset(r["blocked_status"])
    assert sweep_lib.ARTICLE_HINT_RE.pattern == r["article_hints"]["path_pattern"]
    assert sweep_lib.NEVER_CLOSE_PATH_RE.pattern == r["never_close"]["path_patterns"][0]


def test_source_has_no_duplicate_rule_literals():
    """sweep_lib must not re-declare a rule the file already owns.

    Uses the AST so a mention inside a docstring does not trip it -- prose
    explaining a rule is fine, a second copy of the rule is not.
    """
    import ast
    tree = ast.parse((SCRIPTS / "sweep_lib.py").read_text(encoding="utf-8"))
    owned = set()
    for group in ("never_close", "non_article"):
        owned |= set(sweep_lib.RULES[group]["segments"])
    owned |= set(sweep_lib.RULES["paywalled_hosts"])
    owned |= set(sweep_lib.RULES["tracking_params"])

    dupes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in owned:
                dupes.add(node.value)
    assert not dupes, (
        f"these rule values are literals in sweep_lib as well as rules.json "
        f"(the file cannot be the source of truth): {sorted(dupes)}")


# --- fail closed, loudly ---------------------------------------------------

def test_missing_rules_file_raises(tmp_path):
    with pytest.raises(RulesError, match="not found"):
        load_rules(tmp_path / "nope.json")


def test_invalid_json_raises(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(RulesError, match="not valid JSON"):
        load_rules(p)


def test_missing_key_raises_and_names_it(tmp_path):
    rules = dict(sweep_lib.RULES)
    rules.pop("never_close")
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(rules), encoding="utf-8")
    with pytest.raises(RulesError, match="never_close"):
        load_rules(p)


def test_wrong_type_raises(tmp_path):
    rules = dict(sweep_lib.RULES)
    rules["paywalled_hosts"] = "medium.com"
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(rules), encoding="utf-8")
    with pytest.raises(RulesError, match="paywalled_hosts"):
        load_rules(p)


def test_bad_regex_raises_at_load(tmp_path):
    """A broken pattern must fail before a sweep, not mid-run."""
    rules = json.loads(json.dumps(sweep_lib.RULES))
    rules["never_close"]["path_patterns"] = ["([unclosed"]
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(rules), encoding="utf-8")
    with pytest.raises(RulesError, match="bad regex"):
        load_rules(p)


def test_loader_never_falls_back_to_defaults(tmp_path):
    """Silently using built-in rules would close tabs on rules nobody can see."""
    with pytest.raises(RulesError):
        load_rules(tmp_path / "absent.json")


# --- the rules themselves still work --------------------------------------

def test_every_never_close_segment_blocks():
    for seg in sweep_lib.NEVER_CLOSE_SEGMENTS:
        ok, _ = sweep_lib.is_never_close(f"https://s.example/{seg}")
        assert ok, f"{seg} listed but does not block"


def test_every_non_article_segment_demotes():
    for seg in sweep_lib.NON_ARTICLE_SEGMENTS:
        ok, _ = classify_url(f"https://news.example/{seg}", "T")
        assert not ok, f"/{seg} still classifies as an article"


def test_every_paywalled_host_is_recognised():
    for host in sweep_lib.PAYWALLED_HOSTS:
        assert sweep_lib.is_paywalled(f"https://{host}/story"), host


def test_article_hints_still_fire():
    for path in ("/blog/a", "/news/b", "/articles/c", "/2026/10/03/d", "/story/e"):
        ok, _ = classify_url(f"https://news.example{path}", "T")
        assert ok, path


def test_segment_rules_do_not_swallow_substring_articles():
    for path in ("/blog/cartoon-history", "/blog/how-to-edit-video",
                 "/blog/mailman-archive", "/posts/2024/verify-your-backup"):
        ok, _ = classify_url(f"https://news.example{path}", "T")
        assert ok, path
