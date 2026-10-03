"""Deterministic core for article-sweeper.

Covers the guarantees that must NOT depend on agent prose:
URL unwrap + canonicalize, dedupe, article classification baseline,
CDP parsing/validation, close-candidate revalidation, atomic append,
port/host validation, per-browser endpoint registry, log redaction.

All functions are pure/std-lib-only so CI can test them with mocks.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Ports / hosts
# ---------------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"

#: Default debug port per Chromium-family browser. A single global 9222
#: cannot serve several browsers at once, so each browser gets its own.
DEFAULT_PORTS = {
    "thorium": 9222,
    "chromium": 9223,
    "chrome": 9224,
    "brave": 9225,
    "edge": 9226,
    "vivaldi": 9227,
    "opera": 9228,
}


def validate_port(port) -> int:
    """Validate a TCP port. Raises ValueError unless 1 <= port <= 65535."""
    try:
        n = int(str(port).strip())
    except (TypeError, ValueError):
        raise ValueError(f"bad port: {port!r}")
    if not 1 <= n <= 65535:
        raise ValueError(f"bad port (want 1-65535): {port!r}")
    return n


def validate_host(host: str) -> str:
    """Only loopback hosts are supported by design (least privilege)."""
    h = (host or "").strip()
    if h not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"refusing non-loopback CDP host {h!r}: "
            "remote debugging exposes the logged-in session; "
            "only 127.0.0.1/localhost/::1 are supported"
        )
    return "127.0.0.1" if h == "localhost" else h


def cdp_host_for_url(host: str) -> str:
    """Bracket an IPv6 loopback host for URL construction ([::1])."""
    h = validate_host(host)
    return f"[{h}]" if ":" in h and not h.startswith("[") else h


def cdp_url(host: str, port, path: str) -> str:
    """Build a CDP loopback URL with correct IPv6 bracketing."""
    # CDP loopback-only by design (validate_host); no HTTPS endpoint exists.
    return f"http://{cdp_host_for_url(host)}:{validate_port(port)}{path}"  # NOSONAR python:S5332


def find_free_port(exclude: set[int] | None = None) -> int:
    """Return a free loopback TCP port (per-browser endpoint discovery).

    TOCTOU note: the socket is released before return, so another process
    can claim the port before the browser binds it. This function is only
    a *hint* for which port to try — it is NOT the isolation guarantee.
    The guarantee is check_endpoint_identity() (plus --browser, plus the
    optional process check) run immediately before operating: a port that
    was reclaimed by something else fails validation and the run aborts.
    """
    exclude = exclude or set()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    if port in exclude:
        return find_free_port(exclude)
    return port


def endpoint_for(browser: str, taken: set[int] | None = None) -> tuple[str, int]:
    """Return (host, port) for a browser, avoiding collisions in `taken`.

    The caller's `taken` set is updated in place with the allocated port,
    so repeated calls accumulate (allocate-then-record in one step).

    This is still only a hint (see find_free_port TOCTOU note): after
    launching the browser on the returned port, confirm with
    wait_for_endpoint() + check_endpoint_identity() (+ verify_endpoint_process
    where supported) and retry with iter_candidate_ports() on failure.
    """
    if taken is None:
        taken = set()
    preferred = DEFAULT_PORTS.get(browser.lower(), 0)
    if preferred and preferred not in taken:
        taken.add(preferred)
        return DEFAULT_HOST, preferred
    port = find_free_port(taken)
    taken.add(port)
    return DEFAULT_HOST, port


def iter_candidate_ports(browser: str, taken: set[int] | None = None,
                         limit: int = 8):
    """Yield (host, port) candidates for launching a browser, most-preferred
    first, recording each into `taken` so retries never repeat a port."""
    if taken is None:
        taken = set()
    for _ in range(max(1, limit)):
        yield endpoint_for(browser, taken)


def wait_for_endpoint(host: str, port: int, *, expect_browser: str = "",
                      timeout: float = 15.0, poll_interval: float = 0.25,
                      fetch_version=None) -> dict:
    """Poll /json/version until the endpoint answers (browser launch handshake).

    Binding a discovered port races with other processes, so a launch is
    only complete when the endpoint itself reports back: poll until
    check_endpoint_identity() passes or `timeout` seconds elapse, then
    raise ValueError. With `expect_browser`, the product must also match
    (strict identity, not just reachability). `fetch_version` injects a
    stub for tests.
    """
    import time as _time
    deadline = _time.time() + max(0.1, timeout)
    last_err: Exception | None = None
    while True:
        try:
            return check_endpoint_identity(
                host, port, expect_browser=expect_browser,
                fetch_version=fetch_version)
        except ValueError as exc:
            last_err = exc
        if _time.time() >= deadline:
            raise ValueError(
                f"endpoint {host}:{port} did not verify within {timeout}s: "
                f"{last_err}")
        _time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# URL unwrap / canonicalize
# ---------------------------------------------------------------------------

#: Query params that are pure tracking and safe to drop. Everything else is
#: preserved: pagination, language, revision, content-id params are meaningful.
#: Deliberately conservative: generic names like `ref`, `referrer`, `spm`
#: are NOT here — they are meaningful on some sites (section refs, vendor
#: params), and dropping them would falsely merge distinct articles.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "gclid", "gbraid", "wbraid", "fbclid", "msclkid", "mc_cid",
    "mc_eid", "igshid", "_hsenc", "_hsmi",
    "srsltid", "vero_conv", "vero_id", "yclid", "ttclid",
})


def _is_tracking_param(name: str) -> bool:
    """Case-insensitive tracking-param test (`UTM_SOURCE` == `utm_source`)."""
    low = (name or "").lower()
    return low in TRACKING_PARAMS or low.startswith("utm_")

#: Query params that must never appear in logs/scratch (tokens, invites).
SENSITIVE_PARAMS = frozenset({
    "token", "auth", "auth_token", "access_token", "id_token", "session",
    "sessionid", "session_id", "key", "api_key", "apikey", "secret",
    "password", "passwd", "invite", "invite_code", "code", "state",
    "sig", "signature", "token_type", "bearer",
})

WRAPPER_DOMAINS = (
    "google.com", "www.google.com",
    "tracking.tldrnewsletter.com", "tracking.inflection.io",
)

#: Path shapes that mark a URL as a redirect endpoint (as opposed to an
#: article that merely carries a `url=`-style parameter). The generic
#: redirect-param scan below ONLY runs for WRAPPER_DOMAINS or these paths,
#: so `https://example.com/article?url=https://other.com` is never
#: rewritten. `lmu…` covers kit-mail click-tracking paths.
REDIRECT_PATH_RE = re.compile(
    r"/(url|redirect|redir|r|l|click|track/click|CL0|lmu\w*)(/|$|\?)",
    re.I,
)

#: Param names that may carry a redirect target on a redirect endpoint.
REDIRECT_PARAMS = ("u", "url", "redirect", "dest", "destination", "to")


def unwrap_tracking_wrapper(url: str) -> str:
    """Unwrap known redirect/tracking wrappers deterministically.

    Scoped by design: the generic redirect-param scan only applies to
    known wrapper domains or redirect-shaped paths. Arbitrary article
    URLs that happen to carry a `url=`/`to=` parameter are returned
    unchanged (rewriting them would corrupt canonicalization, dedupe,
    classification, and close authorization).
    """
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return url
    host = (p.hostname or "").lower()
    qs = urllib.parse.parse_qs(p.query, keep_blank_values=True)

    # google.com/url?q=<real>
    if host in ("google.com", "www.google.com") and p.path == "/url":
        real = (qs.get("q") or qs.get("url") or [None])[0]
        if real and re.match(r"https?://", real):
            return real
    # tldr / inflection click-tracking: first absolute-URL param wins
    if host in ("tracking.tldrnewsletter.com", "tracking.inflection.io"):
        for vals in qs.values():
            for v in vals:
                if re.match(r"https?://", v or ""):
                    return v
        # path-embedded target e.g. /CL0/https://example.com/...
        m = re.search(r"/(https?://.+)$", p.path)
        if m:
            return urllib.parse.unquote(m.group(1))
    # Generic redirect params (`?u=`, `?url=`, `?redirect=`, …): ONLY on
    # known wrapper domains or redirect-shaped paths (kit-mail `lmu…`
    # paths included). Never on arbitrary hosts.
    if host in WRAPPER_DOMAINS or REDIRECT_PATH_RE.search(p.path or ""):
        for key in REDIRECT_PARAMS:
            vals = qs.get(key) or []
            for v in vals:
                if re.match(r"https?://", v or ""):
                    return v
    return url


def canonicalize_url(url: str) -> str:
    """Canonical form used for dedupe.

    - unwraps tracking wrappers first
    - lowercases scheme+host, drops default ports
    - drops fragment
    - drops ONLY known tracking params; keeps everything else (pagination,
      lang, revision, content ids) so distinct articles never merge
    - sorts remaining query pairs for stability
    """
    url = unwrap_tracking_wrapper(url.strip())
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return url
    scheme = (p.scheme or "https").lower()
    host = (p.hostname or "").lower()
    if not host:
        return url
    port = p.port
    if port and not ((scheme == "http" and port == 80)
                     or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = p.path or "/"
    # keep meaningful query, drop tracking-only params
    pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    kept = sorted(
        (k, v) for k, v in pairs
        if not _is_tracking_param(k)
    )
    query = urllib.parse.urlencode(kept, doseq=True)
    out = urllib.parse.urlunparse((scheme, host, path, "", query, ""))
    return out


def dedupe_key(url: str) -> str:
    """Dedupe identity = canonical URL (query-significant except tracking)."""
    return canonicalize_url(url)


def dedupe_tabs(tabs: list[dict]) -> dict[str, list[dict]]:
    """Group tab records by canonical URL. Returns {canonical: [tabs]}."""
    groups: dict[str, list[dict]] = {}
    for t in tabs:
        key = dedupe_key(str(t.get("url", "")))
        groups.setdefault(key, []).append(t)
    return groups


# ---------------------------------------------------------------------------
# Log redaction
# ---------------------------------------------------------------------------

def redact_url(url: str) -> str:
    """Redact sensitive values for logs/scratch output.

    Covers query params (SENSITIVE_PARAMS), URL fragment (OAuth-style
    ``#access_token=...`` leaks), userinfo (``user:pass@host``), and
    path segments that look like tokens (long hex/base64/jwt-like).
    Callers must still prefer --redact + scratch cleanup; redaction is
    best-effort, not a guarantee that a URL is safe to share.
    """
    try:
        p = urllib.parse.urlparse(url)
        pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        red = [
            (k, "***" if k.lower() in SENSITIVE_PARAMS else v)
            for k, v in pairs
        ]
        query = urllib.parse.urlencode(red, doseq=True)
        # Fragment: redact whole fragment when it carries token-like keys
        # (e.g. #access_token=abc&token_type=bearer).
        frag = p.fragment
        if frag:
            fpairs = urllib.parse.parse_qsl(frag, keep_blank_values=True)
            if any(k.lower() in SENSITIVE_PARAMS or k.lower() in
                   ("access_token", "refresh_token") for k, _ in fpairs):
                frag = "&".join(
                    f"{k}=***" if k.lower() in SENSITIVE_PARAMS or k.lower()
                    in ("access_token", "refresh_token") else f"{k}={v}"
                    for k, v in fpairs) if fpairs else "***"
            elif len(frag) >= 20 and re.fullmatch(r"[A-Za-z0-9\-_%.~+/=]+", frag):
                frag = "***"
        # Userinfo: never log passwords/tokens in user:pass@host.
        netloc = p.netloc
        if "@" in netloc:
            userinfo, _, hostport = netloc.rpartition("@")
            if ":" in userinfo or userinfo.lower() in SENSITIVE_PARAMS:
                netloc = "***@" + hostport
            elif len(userinfo) >= 16:
                netloc = "***@" + hostport
        # Path: mask long hex/base64/jwt-looking segments (invite codes,
        # share tokens) while keeping human-readable slugs.
        def _mask_seg(seg: str) -> str:
            if len(seg) >= 24 and re.fullmatch(
                    r"[A-Za-z0-9\-_+/=]+", seg):
                # JWT has dots; check separately below. Heuristic: high
                # entropy + length => likely token.
                letters = sum(c.isalpha() for c in seg)
                digits = sum(c.isdigit() for c in seg)
                if letters >= 8 and (digits >= 4 or "-" in seg or "_" in seg
                                     or len(seg) >= 32):
                    return "***"
            if seg.count(".") == 2 and len(seg) >= 24 and re.fullmatch(
                    r"[A-Za-z0-9\-_]+(\.[A-Za-z0-9\-_]+){2}", seg):
                return "***"  # JWT-shaped
            return seg
        path = "/".join(_mask_seg(s) for s in p.path.split("/"))
        return urllib.parse.urlunparse(
            (p.scheme, netloc, path, p.params, query, frag))
    except Exception:
        return "<unparseable-url>"


def redact_record(rec: dict) -> dict:
    out = dict(rec)
    if "url" in out and isinstance(out["url"], str):
        out["url"] = redact_url(out["url"])
    return out


# ---------------------------------------------------------------------------
# Article classification (deterministic baseline; agent makes final call)
# ---------------------------------------------------------------------------

# Host blocklist: matched against the hostname with dot boundaries (exact
# or subdomain-suffix), never by raw substring — otherwise "ex.com" would
# falsely match an "x.com" rule.
BLOCKED_HOST_EXACT_OR_SUFFIX = frozenset({
    "github.com", "gitlab.com", "bitbucket.org",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "linkedin.com", "reddit.com",
    "outlook.com", "outlook.office.com", "teams.microsoft.com",
    "slack.com", "discord.com", "zoom.us",
    "paypal.com",
})
BLOCKED_HOST_PREFIXES = (
    "mail.google.", "outlook.", "teams.", "slack.", "discord.",
    "meet.google", "calendar.google", "drive.google", "sheets.",
    "slides.", "accounts.google", "login.", "auth.", "sso.",
    "grafana.", "kibana.", "jira.", "linear.", "asana.", "trello.",
    "notion.",
)
BLOCKED_HOST_SUBSTRINGS = ("pornhub", "xnxx", "xvideos", "bank")


def _host_blocked(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    if not h:
        return False
    if h in ("localhost",):
        return True
    try:
        import ipaddress
        ipaddress.ip_address(h)
        return True  # raw IPs (incl. 127.0.0.1) are app/local, not articles
    except ValueError:
        pass
    for b in BLOCKED_HOST_EXACT_OR_SUFFIX:
        if h == b or h.endswith("." + b):
            return True
    for p in BLOCKED_HOST_PREFIXES:
        if h.startswith(p):
            return True
    return any(s in h for s in BLOCKED_HOST_SUBSTRINGS)

NON_ARTICLE_PATH_RE = re.compile(
    r"(/inbox|/mail|/chat|/chats|/messages|/feed$|/feeds/|/dashboard|"
    r"/settings|/account|/billing|/admin|/repos?/|/pulls?/|/issues?/|"
    r"/pipelines|/actions|/deploy|/checkout|/cart|/login|/signin|/signup|"
    r"/oauth|/sso|/status$|/healthz)",
    re.I,
)

INTERNAL_SCHEMES = (
    "devtools://", "chrome://", "chrome-extension://", "edge://",
    "brave://", "opera://", "vivaldi://", "about:", "view-source:",
)

ARTICLE_HINT_RE = re.compile(
    r"(/blog/|/blogs/|/news/|/articles?/|/posts?/|/stories?/|/docs?/|"
    r"/tutorials?/|/guides?/|/papers?/|/releases?/|/changelog|/wiki/|"
    r"/20\d\d/\d\d/|/p/|/story/|/essays?/)",
    re.I,
)


def classify_url(url: str, title: str = "") -> tuple[bool, str]:
    """Baseline classifier: (is_article_candidate, reason).

    Heuristic only — the agent still applies SKILL.md judgment, but this
    gives deterministic, tested behavior instead of prose-only rules.
    """
    u = (url or "").strip()
    low = u.lower()
    for scheme in INTERNAL_SCHEMES:
        if low.startswith(scheme):
            return False, f"internal-scheme:{scheme}"
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return False, "unparseable-url"
    if p.scheme not in ("http", "https"):
        return False, f"non-http-scheme:{p.scheme}"
    host = (p.hostname or "").lower()
    if _host_blocked(host):
        return False, "non-article-host"
    if NON_ARTICLE_PATH_RE.search(p.path):
        return False, "non-article-path"
    if ARTICLE_HINT_RE.search(p.path):
        return True, "article-path-hint"
    # default: candidate article; agent confirms (title/content check)
    if title and len(title.strip()) > 0 and "." in host:
        return True, "default-candidate"
    return False, "weak-signal"


# ---------------------------------------------------------------------------
# Typed classification (customer feedback #3)
#
# The prose classifier cost real money and seconds per sweep. This is the
# cheap gate that runs first: one pass, no generation, no tokens. It returns
# a *typed* decision the agent can branch on, and only the surviving
# `article` entries ever reach the expensive summarizer.
# ---------------------------------------------------------------------------

ARTICLE = "article"
LEAVE_OPEN = "leave-open"
DUPLICATE_OF = "duplicate-of"
# Explicit "cannot tell" outcome (customer feedback #2/#3). Heuristics that only
# reach `default-candidate` land here instead of silently becoming articles: a
# wrong `article` is the one irreversible mistake in a sweep, because the tab
# closes. `unsure` never closes — it goes to agent judgment or stays open.
UNSURE = "unsure"

# Only these reasons are strong enough to authorize a close on their own.
STRONG_ARTICLE_REASONS = frozenset({"article-path-hint", "user-named"})


@dataclass(frozen=True)
class TabDecision:
    """Typed, auditable decision for one tab. No prose anywhere."""

    url: str
    decision: str
    reason: str
    canonical: str
    duplicate_of: str = ""      # canonical URL of the kept tab, when duplicated
    title: str = ""
    tab_ids: tuple[str, ...] = ()

    @property
    def is_article(self) -> bool:
        return self.decision == ARTICLE

    @property
    def is_closable(self) -> bool:
        """May this tab be closed? `unsure` never is."""
        return self.decision in (ARTICLE, DUPLICATE_OF)

    @property
    def needs_judgment(self) -> bool:
        return self.decision == UNSURE

    def __iter__(self):
        """Unpack as the documented (decision, url) pair."""
        return iter((self.decision, self.url))


def _tab_fields(tab):
    """Accept TabRecord or dict; return (url, title, tab_id)."""
    if isinstance(tab, TabRecord):
        return tab.url, tab.title, tab.id
    if isinstance(tab, dict):
        return (str(tab.get("url", "")),
                str(tab.get("title", "") or ""),
                str(tab.get("id", "") or ""))
    raise TypeError(f"tab must be TabRecord or dict, got {type(tab).__name__}")


def classify_tabs_typed(
    tabs: list,
    *,
    user_named: frozenset[str] = frozenset(),
) -> list[TabDecision]:
    """Classify every tab in one pass into a typed decision.

    `tabs` accepts `TabRecord` objects or dicts with url/title/id. Returns one
    `TabDecision` per tab, in input order.

    Duplicates: tabs sharing a canonical URL collapse — the first is the keeper
    and every later one is `duplicate-of:<canonical>`, pointing at the keeper so
    all of them still close together. Near-duplicates the canonicalizer does not
    merge (AMP, share tokens, author subdomains) are NOT collapsed here; they
    surface as separate `article` decisions for the agent to judge.
    """
    seen: dict[str, TabDecision] = {}
    out: list[TabDecision] = []
    for tab in tabs:
        url, title, tab_id = _tab_fields(tab)
        canonical = dedupe_key(url)
        prior = seen.get(canonical)
        if prior is not None:
            out.append(TabDecision(
                url=url, decision=DUPLICATE_OF, reason="canonical-duplicate",
                canonical=canonical, duplicate_of=prior.url, title=title,
                tab_ids=(tab_id,) if tab_id else (),
            ))
            continue

        if canonical in user_named:
            decision, reason = ARTICLE, "user-named"
        else:
            is_article, reason = classify_url(url, title)
            if not is_article:
                decision = LEAVE_OPEN
            elif reason in STRONG_ARTICLE_REASONS:
                decision = ARTICLE
            else:
                # `default-candidate` means the heuristics only guessed. Guessing
                # `article` here is what turns a misfiled newsletter into a
                # closed tab, so it becomes `unsure` and waits for judgment.
                decision = UNSURE
        d = TabDecision(
            url=url, decision=decision, reason=reason, canonical=canonical,
            title=title, tab_ids=(tab_id,) if tab_id else (),
        )
        seen[canonical] = d
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Paywall / blocked-host policy (customer feedback #4 and #5)
# ---------------------------------------------------------------------------

PAYWALLED_HOSTS = frozenset({
    "medium.com", "nytimes.com", "wsj.com", "bloomberg.com",
    "theatlantic.com", "economist.com", "ft.com", "washingtonpost.com",
    "newyorker.com", "sfgate.com", "seattletimes.com", "bostonglobe.com",
    "chicagotribune.com", "latimes.com", "telegraph.co.uk", "thetimes.co.uk",
    "wsj.eu", "arstechnica.com", "techcrunch.com", "wired.com",
})

# Statuses meaning "the publisher refused", not "the page moved".
BLOCKED_STATUS = frozenset({401, 402, 403, 451})

SKIP_FETCH_TITLE_ONLY = "skip-fetch-title-only"
FETCH_THEN_SEARCH = "fetch-then-search-on-block"
FETCH = "fetch"


def is_paywalled(url: str) -> bool:
    """True when the host is known to block or paywall direct fetches."""
    try:
        host = (urllib.parse.urlparse(url or "").hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in PAYWALLED_HOSTS)


def plan_fetch(url: str, *, allow_search_fallback: bool = False) -> str:
    """Decide how to get an article body *before* spending a request.

    Returns SKIP_FETCH_TITLE_ONLY, FETCH_THEN_SEARCH or FETCH.

    Feedback #5: paywalled domains skip the search fallback by default. A
    search-based summary of a paywalled article is usually worse than an honest
    title+domain line, and each fallback costs a round trip. Opt in per sweep
    with `allow_search_fallback=True`.
    """
    if is_paywalled(url):
        return FETCH_THEN_SEARCH if allow_search_fallback else SKIP_FETCH_TITLE_ONLY
    return FETCH


def is_blocked_status(status) -> bool:
    """True for HTTP statuses that mean 'publisher refused'."""
    try:
        return int(status) in BLOCKED_STATUS
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Content-based refinement (customer feedback: fetch once, use it for both)
#
# The URL gate already excludes webmail, repos and dashboards, so the bodies we
# fetch are the ones we intended to read. That makes the fetched text free
# evidence for the decision that is still open: text density and readability
# beat guessing from a title, and it costs no extra request.
#
# Deliberately conservative. It may ONLY promote `unsure` -> `article`, never
# touch `leave-open`, and never demote. A web inbox has plenty of text; if
# refinement could promote `leave-open`, the densest dashboard in a sweep would
# become the tab that closes.
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.I | re.S)
_BLOCK_RE = re.compile(
    r"</(p|div|section|article|li|h[1-6]|blockquote|tr)>|<br\s*/?>", re.I)


def html_to_text(html: str) -> str:
    """Very small HTML -> text reducer. No dependency, no cleverness.

    Block-level closers become newlines before tags are stripped — otherwise the
    whole document collapses to one line and the paragraph-based density signals
    see a single blob.
    """
    if not html:
        return ""
    text = _SCRIPT_RE.sub(" ", html)
    text = _BLOCK_RE.sub("\n\n", text)
    text = _TAG_RE.sub(" ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def content_signals(text: str) -> dict:
    """Cheap density/readability metrics. No model, no tokens."""
    t = (text or "").strip()
    words = re.findall(r"\w[\w'’-]*", t)
    n_words = len(words)
    paragraphs = [p for p in re.split(r"\n{2,}", t) if len(p.split()) >= 12]
    # Link density: how much of the text is inside <a>. High density = index or
    # nav, not an article. Computed on the raw HTML when available.
    long_words = sum(1 for w in words if len(w) >= 7)
    sentences = len(re.findall(r"[.!?](?:\s|$)", t))
    avg_sentence = (n_words / sentences) if sentences else 0.0
    return {
        "chars": len(t),
        "words": n_words,
        "paragraphs": len(paragraphs),
        "avg_sentence_words": round(avg_sentence, 1),
        "long_word_ratio": (long_words / n_words) if n_words else 0.0,
    }


def link_density(html: str) -> float:
    """Fraction of visible text that sits inside anchors. 0.0-1.0."""
    if not html:
        return 1.0
    total = len(html_to_text(html))
    if total == 0:
        return 1.0
    anchor_text = sum(len(html_to_text(m)) for m in re.findall(r"<a\b[^>]*>(.*?)</a>", html, re.I | re.S))
    return min(1.0, anchor_text / total)


# Thresholds chosen so an article-shaped body passes and an index/nav/shell does
# not. Deliberately conservative: a false negative only leaves a tab open.
MIN_WORDS = 220
MIN_PARAGRAPHS = 3
MIN_LONG_WORD_RATIO = 0.12
MAX_LINK_DENSITY = 0.45


def looks_like_article(text: str, *, html: str = "") -> tuple[bool, str]:
    """(is_article_body, reason) from fetched content alone."""
    s = content_signals(text)
    if s["words"] < MIN_WORDS:
        return False, f"thin-content:{s['words']}w"
    if s["paragraphs"] < MIN_PARAGRAPHS:
        return False, f"few-paragraphs:{s['paragraphs']}"
    if s["long_word_ratio"] < MIN_LONG_WORD_RATIO:
        return False, f"low-word-variety:{round(s['long_word_ratio'], 3)}"
    if html and link_density(html) > MAX_LINK_DENSITY:
        return False, f"link-heavy:{round(link_density(html), 2)}"
    return True, (f"dense-content:{s['words']}w/{s['paragraphs']}p")


def refine_by_content(decision: TabDecision, text: str, *,
                      html: str = "") -> TabDecision:
    """Promote `unsure` -> `article` on strong content evidence. Nothing else.

    - `unsure` + article-shaped body  -> `article`  (now closable)
    - `unsure` + weak body            -> unchanged, stays `unsure`
    - `article` / `leave-open` / `duplicate-of` -> never modified
    """
    if decision.decision != UNSURE:
        return decision
    ok, reason = looks_like_article(text, html=html)
    if not ok:
        return decision
    return TabDecision(
        url=decision.url, decision=ARTICLE, reason=f"content:{reason}",
        canonical=decision.canonical, duplicate_of=decision.duplicate_of,
        title=decision.title, tab_ids=decision.tab_ids,
    )


# ---------------------------------------------------------------------------
# CDP parsing / validation
# ---------------------------------------------------------------------------

@dataclass
class TabRecord:
    id: str
    url: str
    title: str = ""
    type: str = "page"
    endpoint: str = ""          # e.g. "127.0.0.1:9224"
    browser: str = ""           # e.g. "chrome"
    canonical: str = field(default="")

    def __post_init__(self):
        self.canonical = dedupe_key(self.url)


def is_internal_target(url: str, ctype: str = "page") -> bool:
    low = (url or "").lower()
    if any(low.startswith(s) for s in INTERNAL_SCHEMES):
        return True
    # Non-page CDP target types (background_page, service_worker, etc.)
    return ctype != "page"


def parse_cdp_list(data, *, endpoint: str = "", browser: str = "") -> list[TabRecord]:
    """Validate + filter a CDP /json/list payload.

    - requires list of objects with string id + url
    - keeps only type == "page" (robust internal-target filter, not just
      a devtools:// substring check)
    - malformed entries raise ValueError (never silently accepted)
    - every record carries endpoint+browser identity so a bare tab id is
      never ambiguous across simultaneous browsers
    """
    if not isinstance(data, list):
        raise ValueError("CDP /json/list must be a JSON array")
    out: list[TabRecord] = []
    for i, d in enumerate(data):
        if not isinstance(d, dict):
            raise ValueError(f"CDP entry {i}: not an object")
        ctype = d.get("type", "")
        url = d.get("url", "")
        tab_id = d.get("id", "")
        if not isinstance(tab_id, str) or not tab_id:
            raise ValueError(f"CDP entry {i}: missing/invalid id")
        if not isinstance(url, str) or not url:
            raise ValueError(f"CDP entry {i}: missing/invalid url")
        if is_internal_target(url, ctype):
            continue
        out.append(TabRecord(
            id=tab_id, url=url, title=str(d.get("title", "") or ""),
            type=str(ctype), endpoint=endpoint, browser=browser,
        ))
    return out


def verify_close_candidates(candidates: list[TabRecord],
                             current: list[TabRecord]) -> tuple[list[TabRecord], list[str]]:
    """Revalidate close set immediately before closing.

    A tab may navigate/close/reopen between enumeration and close, so each
    candidate must still exist in `current` with the SAME canonical URL.
    Returns (safe_to_close, problems).
    """
    by_id = {t.id: t for t in current if t.endpoint == candidates[0].endpoint} \
        if candidates else {}
    # fall back to endpoint-agnostic map when endpoint unset (legacy files)
    if candidates and not candidates[0].endpoint:
        by_id = {t.id: t for t in current}
    safe, problems = [], []
    for c in candidates:
        now = by_id.get(c.id)
        if now is None:
            problems.append(f"{c.id}: gone before close; skip")
            continue
        if now.canonical != c.canonical:
            problems.append(
                f"{c.id}: navigated since approval "
                f"({redact_url(c.url)} -> {redact_url(now.url)}); skip")
            continue
        safe.append(c)
    return safe, problems


def diff_tab_sets(before: list[TabRecord],
                  after: list[TabRecord],
                  closed_ids: set[str]) -> dict:
    """Before/after set comparison (the 'verify rest stayed open' check)."""
    before_ids = {t.id for t in before}
    after_ids = {t.id for t in after}
    return {
        "closed_expected": sorted(closed_ids),
        "still_open_from_close_set": sorted(closed_ids & after_ids),
        "unexpectedly_closed": sorted((before_ids - after_ids) - closed_ids),
        "remaining": len(after_ids),
    }


def last_page_guard(candidates, live_pages: list[TabRecord]) -> None:
    """Refuse a close set that would leave the browser with zero page tabs.

    Verified live (Windows, Thorium): closing a browser's only page tab
    exits the whole browser process — the sweep loses the endpoint and
    any further tabs it was supposed to leave open. `candidates` are the
    revalidated close records, `live_pages` the fresh pre-close page
    list; the guard fires when closing every candidate would exhaust the
    pages. Note a singleton browser is exactly the case the guard can
    detect with certainty; multi-window "last tab of the last window"
    variants are not visible over CDP and stay out of scope.

    Raises ValueError with an actionable message. Callers that genuinely
    want the shutdown behavior may bypass it explicitly (CLI:
    --allow-last-tab); the default is to refuse.
    """
    cand_ids = {c.id for c in candidates}
    remaining = sum(1 for t in live_pages if t.id not in cand_ids)
    if candidates and remaining == 0:
        raise ValueError(
            f"refusing: closing {len(candidates)} tab(s) would leave "
            f"the browser with zero page tabs; verified live (Thorium) "
            f"this exits the whole browser. Close or leave another tab "
            f"open first, or rerun with --allow-last-tab to accept the "
            f"shutdown.")


# ---------------------------------------------------------------------------
# CDP endpoint identity
# ---------------------------------------------------------------------------

#: Vendor product tokens that identify a browser in /json/version's
#: `Browser` string. Needed because vendors abbreviate: Edge reports
#: `Edg/<ver>`, Opera `OPR/<ver>` — a naive `"edge" in product` check
#: would reject the real Edge endpoint.
BROWSER_PRODUCT_HINTS = {
    "thorium": ("thorium",),
    "chromium": ("chromium", "chrome"),
    "chrome": ("chrome", "chromium"),
    "brave": ("brave",),
    "edge": ("edge", "edg/"),
    "vivaldi": ("vivaldi",),
    "opera": ("opera", "opr/"),
}


def browser_matches_product(browser: str, product: str) -> bool:
    """True when the /json/version product string identifies `browser`."""
    want = (browser or "").strip().lower()
    prod = (product or "").lower()
    if not want:
        return False
    hints = BROWSER_PRODUCT_HINTS.get(want, (want,))
    return any(h in prod for h in hints)

_UA_CHROME_RE = re.compile(r"Chrome/(\d+)")


def _vivaldi_version_mismatch(browser: str, product: str,
                              user_agent: str) -> bool:
    """Vivaldi signature (verified live, Vivaldi 8.2 / Chromium 152):
    /json/version reports ``Browser: Chrome/8.2.4133.68`` — Vivaldi's OWN
    version under a Chrome prefix — while User-Agent shows the real
    Chromium version ``Chrome/152.0.0.0``. No vendor token exists in
    either field. Plain Chrome always reports its Chromium version
    consistently in both fields, so a major-version disagreement between
    the two is distinctive. Conservative by construction: gated to
    ``browser == "vivaldi"`` (never a generic hint), and a missing or
    unparsable version on either side fails closed.
    """
    if (browser or "").strip().lower() != "vivaldi":
        return False
    m_b = _UA_CHROME_RE.search(product or "")
    m_u = _UA_CHROME_RE.search(user_agent or "")
    if not m_b or not m_u:
        return False
    return m_b.group(1) != m_u.group(1)


def _ua_fallback_matches(browser: str, user_agent: str) -> bool:
    """User-Agent fallback for vendors that hide their identity in the
    /json/version ``Browser`` field. Verified live: Opera 136 (Windows)
    reports ``Browser: Chrome/152...`` but keeps ``OPR/136.0.0.0`` in
    User-Agent. Only vendor-distinctive tokens count here — the generic
    chrome/chromium hint is deliberately excluded, so a plain-Chrome
    endpoint can never pass as a branded browser (and vice versa: an
    Opera UA still names Chrome, but Browser already matched in that
    direction before this fallback runs).
    """
    want = (browser or "").strip().lower()
    ua = (user_agent or "").lower()
    if not want or not ua:
        return False
    hints = tuple(
        h for h in BROWSER_PRODUCT_HINTS.get(want, (want,))
        if h not in ("chrome", "chromium")
    )
    if not hints:
        return False
    return any(h in ua for h in hints)


def check_endpoint_identity(host: str, port: int, *,
                            expect_browser: str = "",
                            fetch_version=None,
                            timeout: int = 5) -> dict:
    """Validate a CDP endpoint owns the expected browser before operating.

    Fetches ``/json/version`` and matches ``Browser`` product string against
    ``expect_browser`` (case-insensitive substring on the product name, e.g.
    ``chrome``, ``brave``, ``edge``). If the Browser field does not match,
    the ``User-Agent`` is consulted as a fallback, but only vendor-
    distinctive tokens (``OPR/``, ``Edg/``, ``brave``, ...) may rescue the
    match — a plain-Chrome endpoint cannot pose as a branded browser.
    Returns the version payload dict.

    Raises ValueError on unreachable endpoint / malformed payload /
    browser mismatch. Callers (cdp_close) must run this before any close
    so a stray local CDP service on a reused port cannot be driven by
    mistake.
    """
    import urllib.request as _urlreq
    import json as _json
    host = validate_host(host)
    port = validate_port(port)
    if fetch_version is not None:
        try:
            payload = fetch_version(host, port)
        except Exception as exc:
            raise ValueError(f"CDP endpoint {host}:{port} unreachable: {exc}")
    else:
        # CDP loopback-only by design: validate_host() restricts host to
        # 127.0.0.1/localhost/::1, CDP offers no HTTPS endpoint.
        url = cdp_url(host, port, "/json/version")  # NOSONAR python:S5332
        try:
            with _urlreq.urlopen(url, timeout=timeout) as resp:
                payload = _json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            raise ValueError(f"CDP endpoint {host}:{port} unreachable: {exc}")
    if not isinstance(payload, dict):
        raise ValueError(f"CDP /json/version at {host}:{port} not an object")
    product = str(payload.get("Browser", "") or "")
    if expect_browser:
        want = expect_browser.strip().lower()
        if want and not browser_matches_product(want, product):
            # Some vendors hide their identity from the Browser field
            # (verified live: Opera 136 reports "Chrome/152...") but leave
            # a distinctive token in User-Agent. UA fallback is
            # conservative: only vendor-distinctive tokens may rescue.
            ua = str(payload.get("User-Agent", "") or "")
            if not (_ua_fallback_matches(want, ua)
                    or _vivaldi_version_mismatch(want, product, ua)):
                raise ValueError(
                    f"endpoint {host}:{port} reports Browser={product!r}, "
                    f"expected browser containing {expect_browser!r}; refusing")
    return payload


# ---------------------------------------------------------------------------
# Endpoint process ownership (Linux /proc; optional hardening)
# ---------------------------------------------------------------------------

def _port_inodes(net_tcp_text: str, port: int) -> set[int]:
    """Parse /proc/net/tcp content -> inodes of LISTEN sockets on `port`."""
    inodes: set[int] = set()
    for line in net_tcp_text.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 10:
            continue
        try:
            _ip_hex, port_hex = parts[1].split(":")
            st = parts[3]
            inode = int(parts[9])
        except (ValueError, IndexError):
            continue
        if st.upper() == "0A" and int(port_hex, 16) == port:
            inodes.add(inode)
    return inodes


def _darwin_listening_pids(port: int, *, run=None) -> list[int]:
    """PIDs holding a LISTEN socket on `port` (macOS; `lsof` based).

    The /proc/net/{tcp,tcp6} tables do not exist on macOS, so lsof is the
    equivalent ground truth (`-Fp` prints one `p<pid>` field line per
    process). `run` is injectable for tests. Fail closed: any query
    failure raises RuntimeError; an empty result is a legitimate [].
    """
    import subprocess
    if run is None:
        def run(argv):
            try:
                return subprocess.run(argv, capture_output=True, text=True,
                                      timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"lsof query failed: {exc}")
    port = validate_port(port)
    proc = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"])
    # `lsof` exits 1 with empty output when nothing matches, but it also exits
    # non-zero and writes to stderr when the query itself fails (binary missing,
    # not permitted). Returning [] for the second case would silently disable the
    # process-ownership proof this function exists to provide, so only a clean
    # 0/1 with an empty stderr counts as "nothing is listening".
    stderr = (getattr(proc, "stderr", "") or "").strip()
    if proc.returncode not in (0, 1) or stderr:
        raise RuntimeError(f"lsof query failed (rc={proc.returncode}): {stderr or 'no output'}")
    return sorted({int(ln[1:]) for ln in proc.stdout.splitlines()
                   if ln.startswith("p") and ln[1:].isdigit()})


def find_pids_listening_on(port: int, *, proc_root: str = "/proc") -> list[int]:
    """PIDs holding a LISTEN socket on `port` (Linux /proc; macOS via lsof).

    Product checks prove *which browser family* answers a port, not
    *which process*. When the workflow launched the browser itself, this
    maps the port back to owner PID(s) so the command line (binary,
    --user-data-dir) can be confirmed. Raises RuntimeError when the
    platform lookup itself fails. `proc_root` is injectable for tests
    (only the real default "/proc" routes to lsof on macOS).
    """
    port = validate_port(port)
    if os.name != "posix":
        raise RuntimeError("process lookup needs Linux /proc")
    if (proc_root == "/proc" and sys.platform == "darwin"
            and not os.path.exists(os.path.join(proc_root, "net"))):
        return _darwin_listening_pids(port)
    inodes: set[int] = set()
    for table in ("net/tcp", "net/tcp6"):  # IPv6 listeners live in tcp6
        path = os.path.join(proc_root, table)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue  # table absent (e.g. IPv6 disabled) is not fatal
        inodes |= _port_inodes(text, port)
    if not inodes:
        return []
    want = {f"socket:[{i}]" for i in inodes}
    pids: list[int] = []
    try:
        entries = os.listdir(proc_root)
    except OSError as exc:
        raise RuntimeError(f"cannot list {proc_root}: {exc}")
    for entry in entries:
        if not entry.isdigit():
            continue
        fddir = os.path.join(proc_root, entry, "fd")
        try:
            fds = os.listdir(fddir)
        except OSError:
            continue  # raced exit / permission denied
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fddir, fd))
            except OSError:
                continue
            if target in want:
                pids.append(int(entry))
                break
    return sorted(pids)


def _darwin_process_cmdline(pid: int, *, run=None) -> str:
    """Command line of `pid` on macOS via `ps -o args=`. Fail closed."""
    import subprocess
    if run is None:
        def run(argv):
            try:
                return subprocess.run(argv, capture_output=True, text=True,
                                      timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"ps query failed: {exc}")
    proc = run(["ps", "-p", str(int(pid)), "-o", "args="])
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"ps gave no cmdline for pid {pid}")
    return proc.stdout.strip()


def read_process_cmdline(pid: int, *, proc_root: str = "/proc") -> str:
    """NUL-joined command line of `pid` (Linux /proc; macOS via ps)."""
    path = os.path.join(proc_root, str(int(pid)), "cmdline")
    if (proc_root == "/proc" and sys.platform == "darwin"
            and not os.path.exists(path)):
        return _darwin_process_cmdline(pid)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read cmdline of pid {pid}: {exc}")
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _win_listening_pids(port: int, *, run=None) -> list[int]:
    """PIDs holding a LISTEN socket on `port` (Windows only).

    PowerShell Get-NetTCPConnection is the /proc/net/tcp equivalent here;
    injectable via `run` for tests. Raises RuntimeError when the query
    itself fails (PowerShell missing, timeout) — callers fail closed.
    """
    import subprocess
    if run is None:
        def run(argv):
            try:
                return subprocess.run(argv, capture_output=True, text=True,
                                      timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"windows process query failed: {exc}")
    script = ("(Get-NetTCPConnection -LocalPort " + str(validate_port(port)) +
              " -State Listen -ErrorAction SilentlyContinue | "
              "Select-Object -ExpandProperty OwningProcess -Unique)")
    proc = run(["powershell", "-NoProfile", "-Command", script])
    return sorted({int(ln) for ln in proc.stdout.split() if ln.strip().isdigit()})


def _win_process_image(pid: int, *, run=None) -> str:
    """Executable path of `pid` (Windows only; '' when unreadable)."""
    import subprocess
    if run is None:
        def run(argv):
            try:
                return subprocess.run(argv, capture_output=True, text=True,
                                      timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"windows process query failed: {exc}")
    proc = run(["powershell", "-NoProfile", "-Command",
                f"(Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue).Path"])
    return proc.stdout.strip()


def verify_endpoint_process_windows(port: int, expect_cmd: str, *,
                                    list_pids=None, image_of=None) -> int:
    """Windows process-ownership proof: some pid listening on `port` must
    run an executable whose path contains `expect_cmd` (e.g. the binary
    name this workflow launched). Strongest identity available on Windows
    and the only one for product-blind vendors (verified live: Thorium
    reports Browser='Chrome/...' with no Thorium token anywhere).
    Injectable `list_pids`/`image_of` for tests; the real implementations
    shell out to PowerShell. Fail closed on any query failure.
    """
    want = (expect_cmd or "").strip().lower()
    if not want:
        raise ValueError("process proof needs a non-empty expected "
                         "command fragment")
    if list_pids is None:
        list_pids = _win_listening_pids
    if image_of is None:
        image_of = _win_process_image
    try:
        pids = list_pids(port)
    except RuntimeError as exc:
        raise ValueError(f"process lookup failed: {exc}")
    if not pids:
        raise ValueError(f"no local process found listening on port {port}")
    for pid in pids:
        try:
            img = image_of(pid)
        except RuntimeError as exc:
            raise ValueError(f"process lookup failed: {exc}")
        if img and want in img.lower():
            return pid
    raise ValueError(
        f"port {port} held by pid(s) {pids} whose executable paths do "
        f"not mention {expect_cmd!r}; refusing")


def verify_endpoint_process(port: int, expect_cmd: str, *,
                            proc_root: str = "/proc") -> int:
    """Confirm a listener on `port` runs a command containing `expect_cmd`.

    Pass a fragment of the launch command this workflow used (binary name
    or `--user-data-dir=…`). Returns the matching PID. Linux matches the
    /proc command line; Windows matches the executable image path (the
    /proc cmdline is not available there). Raises ValueError when nobody
    listens there or no owner's command line/image matches, and
    RuntimeError where process lookup is unsupported. Fail closed.
    """
    want = (expect_cmd or "").strip().lower()
    if not want:
        raise ValueError("verify_endpoint_process needs a non-empty "
                         "expected command fragment")
    try:
        pids = find_pids_listening_on(port, proc_root=proc_root)
    except RuntimeError:
        if os.name != "nt":
            raise
        return verify_endpoint_process_windows(port, expect_cmd)
    if not pids:
        raise ValueError(f"no local process found listening on port {port}")
    for pid in pids:
        try:
            cmd = read_process_cmdline(pid, proc_root=proc_root)
        except RuntimeError:
            continue
        if want in cmd.lower():
            return pid
    raise ValueError(
        f"port {port} held by pid(s) {pids} whose command lines do not "
        f"mention {expect_cmd!r}; refusing")


def confirm_endpoint(host: str, port: int, *, expect_browser: str,
                     expect_cmd: str = "",
                     verify_process=None) -> tuple[dict, int | None, bool]:
    """Shared CLI identity gate. Returns (version_payload, owner_pid,
    product_matched).

    Strict path: /json/version must self-identify as `expect_browser`;
    when `expect_cmd` is given, the listening process must ALSO match
    (strongest proof, mandatory where provided).

    Vendor-blind exception: when the product string can never name the
    requested browser (verified live: Thorium reports plain
    'Chrome/...' with no vendor token anywhere; Windows has no /proc
    cmdline to inspect otherwise) AND `expect_cmd` is provided, a
    passing process-ownership proof substitutes. Both halves are
    required — the proof never stands alone — and generic-family names
    (chrome, chromium), which any Chrome-shaped product satisfies, are
    excluded so a true wrong-endpoint mismatch still refuses.
    `verify_process` injects a stub for tests; default is the real
    `verify_endpoint_process` (Linux /proc cmdline, Windows image path).
    """
    product_matched = True
    try:
        payload = check_endpoint_identity(host, port,
                                          expect_browser=expect_browser)
    except ValueError as exc:
        if (not expect_cmd
                or browser_matches_product(expect_browser, "chrome")):
            raise
        product_matched = False
        payload = {}
    owner = None
    if expect_cmd:
        verifier = verify_process or verify_endpoint_process
        owner = verifier(port, expect_cmd)
    return payload, owner, product_matched


# ---------------------------------------------------------------------------
# Atomic summary-file append (concurrency-safe)
# ---------------------------------------------------------------------------

def _lock_exclusive(fd) -> str:
    """Take an exclusive lock on fd. Returns lock backend name.

    POSIX: fcntl.flock. Windows: msvcrt.locking (1-byte region at
    offset 0). Raises RuntimeError when neither backend exists.
    """
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        return "fcntl.flock"
    except ImportError:
        pass
    try:
        import msvcrt
        # LK_LOCK blocks up to 10s per call; retry for ~30s total.
        import time as _time
        deadline = _time.time() + 30
        while True:
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
                return "msvcrt.locking"
            except OSError:
                if _time.time() >= deadline:
                    raise
                _time.sleep(0.05)
    except ImportError:
        pass
    raise RuntimeError("no file-lock backend (need fcntl or msvcrt)")


def _unlock_exclusive(fd, backend: str) -> None:
    try:
        if backend == "fcntl.flock":
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif backend == "msvcrt.locking":
            import msvcrt
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    except Exception:
        pass


@contextlib.contextmanager
def _summary_lock(path: Path):
    """Exclusive sidecar lock shared by append AND recount-and-rewrite.

    Both writers must take the same `<file>.lock` or a recount rewrite
    can clobber a concurrent append (lost entries). Mandatory on all
    platforms (fcntl / msvcrt); never a silent no-op.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = p.with_suffix(p.suffix + ".lock")
    with open(lock, "a+b") as lockfh:
        backend = _lock_exclusive(lockfh)
        try:
            yield
        finally:
            _unlock_exclusive(lockfh, backend)


def atomic_append(path: Path, lines: list[str]) -> None:
    """Append lines atomically: write-temp-in-same-dir + os.replace for the
    header-create step, then append under an exclusive sidecar lock so
    parallel subagents cannot interleave ('read -> edit -> count' races).

    Locking is mandatory on all platforms: POSIX uses fcntl.flock,
    Windows uses msvcrt.locking. No silent no-op fallback.
    """
    path = Path(path)
    with _summary_lock(path):
        if not path.exists():
            tmp = tempfile.NamedTemporaryFile(
                "w", dir=str(path.parent), delete=False, encoding="utf-8")
            try:
                tmp.write("".join(lines))
                tmp.close()
                os.replace(tmp.name, str(path))
            except BaseException:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise
        else:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("".join(lines))
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass


STREAM_TRAILER_RE = re.compile(
    r"^<!-- sweep-stream: emitted=(?P<emitted>\d+) expected=(?P<expected>\d+)"
    r"(?: (?P<status>complete|partial))? -->$", re.M)


class SummaryStream:
    """Streaming writer for the summary file: classify -> fetch -> summarize
    without rigid batches.

    The failure modes a streaming writer normally introduces -- torn entries,
    partial writes, an untrustworthy count -- are handled structurally rather
    than by hoping:

    * Every entry is appended through atomic_append(), i.e. one locked,
      fsynced write of *complete* lines. A crash mid-stream therefore leaves
      a valid file containing only whole entries; an entry can never be half
      written, because the entry is fully rendered before the write starts.
    * `expected` is recorded in the header *before* any entry lands, and
      recounted against the real entry count at finalize(). If the process
      dies mid-stream the header says what the run intended to produce and
      the file shows what it did, so a truncated run is self-evident instead
      of silently short.
    * finalize() rewrites the count to the truth and appends a machine
      readable `<!-- sweep-stream: ... -->` trailer, so a resumed or audited
      run can be checked without re-parsing prose.

    Ordering is completion order, not source order. That is the point: an
    entry is durable the moment it is summarized, so losing the process
    later cannot lose earlier work.
    """

    def __init__(self, path: Path, expected: int, *, header_extra: str = ""):
        self.path = Path(path)
        self.expected = max(0, int(expected))
        self.emitted = 0
        self.failed: list[str] = []
        self._ensure_header(header_extra)

    def _ensure_header(self, header_extra: str) -> None:
        """Create the file with the intended count; never clobber existing work.

        Re-running a sweep against an existing file must not reset the count to
        this run's `expected`, so an existing header is left for finalize() to
        reconcile.
        """
        with _summary_lock(self.path):
            if self.path.exists():
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                f"# Tab sweep summary\n\n{self.expected} article tabs summarised. "
                f"{header_extra}\n".rstrip() + "\n",
                encoding="utf-8")

    def emit(self, entry_lines: list[str], *, url: str = "") -> None:
        """Append one complete entry durably.

        Never raises on append failure -- a lost entry must not abort the
        sweep, but it is recorded as failed and makes the run `partial`.
        """
        text = "".join(entry_lines)
        if not text.startswith("## "):
            raise ValueError("summary entries must start with '## '")
        try:
            atomic_append(self.path, entry_lines)
        except OSError as exc:  # noqa: PERF203 - failure is recorded, not raised
            self.failed.append(f"{url or text[:60]} ({exc})")
            return
        self.emitted += 1
        # Keep the running count honest so an interrupted run is readable.
        recount_and_fix_header(self.path)

    def finalize(self) -> dict:
        """Reconcile the count to reality and write the stream trailer.

        Returns the manifest, including whether the run completed. `partial`
        means the stream ended with entries missing, which is the signal to
        re-run the remaining URLs rather than close tabs on a short summary.
        """
        true_count = recount_and_fix_header(self.path)
        complete = true_count >= self.expected and not self.failed
        trailer = (f"<!-- sweep-stream: emitted={true_count} "
                   f"expected={self.expected} "
                   f"{'complete' if complete else 'partial'} -->")
        with _summary_lock(self.path):
            existing = (self.path.read_text(encoding="utf-8")
                        if self.path.exists() else "")
            if not STREAM_TRAILER_RE.search(existing):
                if not existing:
                    sep = ""
                elif existing.endswith("\n\n"):
                    sep = ""
                elif existing.endswith("\n"):
                    sep = "\n"
                else:
                    sep = "\n\n"
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(sep + trailer + "\n")
        return {
            "emitted": true_count,
            "expected": self.expected,
            "complete": complete,
            "failed": list(self.failed),
            "path": str(self.path),
        }


def recount_entries(path: Path) -> int:
    """Authoritative recount of '## ' entries (fixes mutable batch counts)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    return sum(1 for line in text.splitlines() if line.startswith("## "))


HEADER_COUNT_RE = re.compile(r"^(\d+)(\s+article tabs summarised\.)", re.M)


def recount_and_fix_header(path: Path) -> int:
    """Recount '## ' entries AND rewrite the header count line to match.

    Batch appends each carry mutable "N article tabs summarised" metadata,
    so the header can disagree with the file. This helper counts the
    entries, rewrites the first `<digits> article tabs summarised.` line
    to the true count (atomic temp-file + replace), and returns the
    count. Files without such a header line are left untouched.

    Takes the same sidecar lock as atomic_append(): without it, a
    concurrent append landing between this read and the replace would be
    silently lost. Safe to run while appends are still in flight; the
    final call (after all batches) leaves header == true count.
    """
    p = Path(path)
    with _summary_lock(p):
        try:
            text = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        n = sum(1 for line in text.splitlines() if line.startswith("## "))
        new_text, subs = HEADER_COUNT_RE.subn(
            lambda m: f"{n}{m.group(2)}", text, count=1)
        if not subs:
            return n
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=str(p.parent), delete=False, encoding="utf-8")
        try:
            tmp.write(new_text)
            tmp.close()
            os.replace(tmp.name, str(p))
        except BaseException:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
        return n


# ---------------------------------------------------------------------------
# Firefox session helpers
# ---------------------------------------------------------------------------

MOZLZ4_MAGIC = b"mozLz40\0"


def copy_session_safe(src: Path, scratch: Path, *,
                      retries: int = 5, settle_ms: int = 200) -> Path:
    """Copy a live session file to scratch first; never read live in place.

    Local-operator CLI tool: `src`/`scratch` come from the invoking
    operator (same trust as shell redirection), not remote input.
    Firefox may be writing the source concurrently, so a single
    shutil.copyfile can tear. Mitigation: copy twice and require
    identical bytes (stable snapshot); retry until stable or retries
    run out, then raise. Enforces the documented 'copy first'
    invariant in code instead of relying on the agent remembering.
    The destination name is unique per invocation (pid + random token):
    two simultaneous sweeps must never share one scratch file for
    write/read/cleanup. The `.copy.` marker is preserved so
    cleanup_session_copy() still recognizes it.
    Callers must delete the returned copy when done (see
    cleanup_session_copy()).
    """
    import time as _time
    import uuid as _uuid
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"not a file: {src}")
    scratch = Path(scratch)
    # Trust boundary (S8707): `scratch` comes from the invoking local
    # operator (same trust as shell redirection), never remote input.
    scratch.mkdir(parents=True, exist_ok=True)  # NOSONAR pythonsecurity:S8707
    unique = f"{os.getpid()}.{_uuid.uuid4().hex[:12]}"
    dst = scratch / f"{src.stem}.copy.{unique}{src.suffix}"
    last_err: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            with open(src, "rb") as fh:  # NOSONAR pythonsecurity:S8707
                first = fh.read()
            _time.sleep(settle_ms / 1000.0)
            with open(src, "rb") as fh:  # NOSONAR pythonsecurity:S8707
                second = fh.read()
            if first != second:
                last_err = ValueError("session file changed during copy; retrying")
                continue
            with open(dst, "wb") as out:  # NOSONAR pythonsecurity:S8707
                out.write(second)
                out.flush()
                try:
                    os.fsync(out.fileno())
                except OSError:
                    pass
            if dst.read_bytes() != second:
                last_err = ValueError("scratch copy mismatch; retrying")
                continue
            return dst
        except (OSError, ValueError) as exc:
            last_err = exc
            _time.sleep(settle_ms / 1000.0)
    # Failure path: never leave a partial/stale scratch copy behind.
    try:
        dst.unlink(missing_ok=True)  # NOSONAR pythonsecurity:S8707
    except TypeError:
        if dst.exists():
            dst.unlink()  # NOSONAR pythonsecurity:S8707
    raise ValueError(f"could not get a stable session snapshot of {src}: {last_err}")


def _is_within(child: Path, parent: Path) -> bool:
    """Boundary-checked containment (no substring matching)."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def cleanup_session_copy(path: Path) -> bool:
    """Delete a scratch session copy. Returns True when nothing remains.

    Only deletes paths that are EITHER named like a scratch copy
    (`.copy.` marker from copy_session_safe) OR contained in the system
    temp tree (directory-boundary checked, never substring). Anything
    else is refused (returns False) so a caller bug can never delete an
    arbitrary file. Deletion errors also return False instead of being
    silently swallowed — for session copies holding sensitive URLs the
    caller must warn, not assume cleanup happened.
    """
    try:
        p = Path(path)
    except Exception:
        return False
    name = p.name
    is_copy = ".copy." in name or name.endswith(".copy")
    try:
        in_tmp = _is_within(p, Path(tempfile.gettempdir()))
    except Exception:
        in_tmp = False
    if not (is_copy or in_tmp):
        return False
    try:
        p.unlink(missing_ok=True)  # NOSONAR pythonsecurity:S8707
    except TypeError:
        try:
            if p.exists():
                p.unlink()  # NOSONAR pythonsecurity:S8707
        except OSError:
            return False
    except OSError:
        return False
    try:
        return not p.exists()
    except OSError:
        return False


def decode_mozlz4(raw: bytes) -> dict:
    """Decode mozLz4 payload. Requires `lz4` package (no brittle fallback)."""
    if raw[:8] != MOZLZ4_MAGIC:
        raise ValueError("not a mozilla lz4 session file")
    try:
        import lz4.block
    except ImportError as exc:
        raise RuntimeError(
            "the `lz4` python package is required "
            "(`pip install lz4`); the tail/lz4cat fallback was removed "
            "because it is known-unreliable") from exc
    doc = json.loads(lz4.block.decompress(raw[8:]))
    if not isinstance(doc, dict):
        raise ValueError("session payload decoded but is not a JSON object")
    return doc


def pick_current_entry(tab: dict) -> dict | None:
    """Pick the *selected* entry (index-aware), not blindly the last one.

    Firefox tabs keep navigation history in `entries` with a 1-based
    `index` (or `lastAccessed`). The visible tab is entries[index-1].
    """
    entries = tab.get("entries", []) or []
    if not entries:
        return None
    idx = tab.get("index", len(entries))
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        idx = len(entries)
    idx = max(1, min(idx, len(entries)))
    cand = entries[idx - 1]
    return cand if isinstance(cand, dict) else entries[-1]


def session_freshness(session_path: Path) -> dict:
    """Report staleness signals: mtime + whether a backup snapshot is newer.

    Only `*.jsonlz4` files under sessionstore-backups/ count (lockfiles
    and temp artifacts are ignored). Reports the newest snapshot's name
    so callers can say WHICH file looks fresher, not just that one is.
    """
    p = Path(session_path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"exists": False}
    info: dict = {"exists": True, "mtime": mtime, "backup_newer": False,
                  "newest_backup": None}
    backups = p.parent / "sessionstore-backups"
    newest = 0.0
    newest_name = None
    if backups.is_dir():
        for f in backups.iterdir():
            if not f.name.endswith(".jsonlz4"):
                continue
            try:
                ts = f.stat().st_mtime
            except OSError:
                continue
            if ts > newest:
                newest, newest_name = ts, f.name
    info["backup_newer"] = newest > mtime
    info["newest_backup"] = newest_name if info["backup_newer"] else None
    return info


def endpoint_gone_confirmed(host: str, port: int, attempts: int = 3,
                            timeout: float = 2.0) -> str | None:
    """Classify whether a dev-mode endpoint is gone, and how sure we are.

    Loopback-only by construction: the probe URL comes from `cdp_url()`,
    so a non-loopback host is refused with ValueError exactly like every
    other CDP call.

    Returns one of:
    - "refused": TCP connection refused (RST - nothing listens). For a
      dev-mode endpoint this means the browser process is provably dead.
      Caveat: some Windows firewall configurations silently drop SYN to
      dead ports, which surfaces as a timeout (None) instead - on such
      hosts "refused" is simply never observed.
    - "dead": TCP accepted but no HTTP response (connection closed or
      reset mid-request). The mid-shutdown window of an exiting browser
      looks like this; so does a wedged server, so this is evidence,
      not proof. This is the classification real browser exits produce
      on Windows (the listener's accept socket resets in-flight HTTP).
    - "alive": endpoint answered a full HTTP request.
    - None: inconclusive (timeout, unreachable host) - fail closed.
    """
    url = cdp_url(host, port, "/json/version")
    state: str | None = None
    for _ in range(max(1, attempts)):
        try:
            urllib.request.urlopen(url, timeout=timeout).close()
            return "alive"  # endpoint answering again: not gone
        except urllib.error.HTTPError:
            return "alive"  # HTTP-level error still proves it serves HTTP
        except OSError as exc:
            state = _probe_failure_state(exc, state)
            if state is None:
                return None  # inconclusive: fail closed
    return state


def _probe_failure_state(exc: OSError, prior: str | None) -> str | None:
    """Map one failed endpoint probe to "refused", "dead", or None.

    urlopen normally wraps socket errors in urllib.error.URLError, so the
    inner `reason` is what gets classified. `prior` carries the strongest
    evidence so far: a bare OSError after an already-proven refusal keeps
    that evidence for the caller's retry; anything else unclassifiable is
    inconclusive (None) so callers fail closed.
    """
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ConnectionRefusedError):
            return "refused"
        if isinstance(reason, (ConnectionResetError, ConnectionAbortedError)):
            return "dead"  # accepted-then-reset: dying peer
        return None  # timeout / unknown reason: inconclusive
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        return "dead"
    if prior == "refused":
        return "refused"
    return None
