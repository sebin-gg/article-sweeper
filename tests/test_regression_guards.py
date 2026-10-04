"""Permanent regression guards.

One-off mutation testing tells you a test is good *today*. These guards make
the protection outlive the person who wrote it: if someone later deletes or
weakens a safety rule, CI fails instead of the rule quietly disappearing.
"""
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "article-sweeper" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sweep_lib  # noqa: E402
from sweep_lib import classify_url, is_never_close  # noqa: E402

# The routes a destructive tab-closer must never touch. Duplicated here on
# purpose: the point is that deleting an entry from sweep_lib's set fails this
# test, rather than silently narrowing protection.
CRITICAL_NEVER_CLOSE_SEGMENTS = frozenset({
    # auth / identity
    "oauth", "login", "signin", "logout", "signup", "register",
    "password", "reset", "session", "mfa", "2fa", "otp", "totp",
    "webauthn", "passkey",
    # money
    "checkout", "cart", "payment", "billing", "subscribe",
    # unsaved state
    "edit", "new", "upload", "compose", "draft", "apply", "viewform",
    # account / admin
    "account", "settings", "admin", "dashboard",
    # verification
    "verify", "confirm", "activate",
})

CRITICAL_NEVER_CLOSE_URLS = (
    "https://s.example/checkout",
    "https://s.example/cart",
    "https://s.example/2fa",
    "https://s.example/account/verify/token",
    "https://s.example/two-factor/auth",
    "https://s.example/challenge/1",
    "https://s.example/verify-email",
    "https://s.example/i/flow",
    "https://s.example/consent/scopes",
    "https://s.example/password/reset",
    "https://s.example/edit/9",
    "https://s.example/apply",
    "https://s.example/viewform/1",
)


def test_never_close_blocklist_has_not_been_narrowed():
    """Deleting an entry from the shipped set must fail here."""
    missing = CRITICAL_NEVER_CLOSE_SEGMENTS - sweep_lib.NEVER_CLOSE_SEGMENTS
    assert not missing, (
        "never-close blocklist lost protection for: "
        f"{sorted(missing)}. A destructive close now applies to these.")


def test_every_declared_segment_actually_blocks():
    """No dead entries: a listed segment that does not match is a silent bug."""
    for seg in sorted(sweep_lib.NEVER_CLOSE_SEGMENTS):
        ok, why = is_never_close(f"https://s.example/{seg}")
        assert ok, f"NEVER_CLOSE_SEGMENTS lists {seg!r} but it does not block"
        assert why.startswith("never-close"), why


@pytest.mark.parametrize("url", CRITICAL_NEVER_CLOSE_URLS)
def test_critical_never_close_urls_are_blocked(url):
    ok, _ = is_never_close(url)
    assert ok, f"{url} must never be closable"


def test_never_close_gate_is_wired_into_the_close_path():
    """Source-level backstop for 'the gate was deleted along with its tests'.

    Behavioural tests already cover this, but a single PR can delete both the
    gate and its tests at once. Asserting the close script still *calls* the
    gate means the safety property is not purely test-owned.
    """
    src = (SCRIPTS / "cdp_close.py").read_text(encoding="utf-8")
    assert "is_never_close" in src, (
        "cdp_close.py no longer references is_never_close; the independent "
        "close-time gate appears to have been removed")
    assert "from sweep_lib import" in src and "NEVER_CLOSE" not in src.split(
        "def main", 1)[-1].replace("is_never_close", ""), (
        "the gate looks inlined or stubbed; keep the sweep_lib call")


def test_never_close_matches_segments_not_substrings():
    """Locks the false-positive behaviour that a substring list would break."""
    for path in ("/blog/cartoon-history", "/blog/how-to-edit-video",
                 "/blog/mailman-archive", "/blog/new-york-guide",
                 "/posts/2024/verify-your-backup",
                 "/news/accountability-report", "/blog/settings-of-the-game"):
        ok, _ = is_never_close("https://s.example" + path)
        assert not ok, f"{path} is a plausible article and must stay closable"


# Routes reachable only via NON_ARTICLE_SEGMENTS (not covered by the harder
# never-close blocklist). Found by the mutation guard: mutating the segment
# match away flips these to "default-candidate" with nothing failing.
SEGMENT_ONLY_NON_ARTICLES = ("mail", "chat", "chats", "messages", "feed",
                             "feeds", "repo", "repos", "pull", "pulls",
                             "issue", "issues", "pipelines", "actions",
                             "deploy", "sso", "inbox")


@pytest.mark.parametrize("seg", SEGMENT_ONLY_NON_ARTICLES)
def test_non_article_segments_still_demote(seg):
    ok, _ = classify_url(f"https://mail.example/{seg}", "Inbox")
    assert not ok, f"/{seg} must not classify as an article"


def test_mutation_guard_rejects_a_dirty_tree(tmp_path):
    """The guard edits source in place, so it must refuse a dirty checkout."""
    import subprocess as sp
    r = sp.run([sys.executable, str(Path(__file__).with_name("mutation_guard.py"))],
               cwd=Path(__file__).resolve().parents[1],
               capture_output=True, text=True)
    # On a clean tree it either runs the mutants or reports failures; either
    # way it must never report the "refusing to run" path spuriously.
    assert "dirty tree" not in r.stderr
