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
import tempfile
import urllib.parse
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

def check_endpoint_identity(host: str, port: int, *,
                            expect_browser: str = "",
                            fetch_version=None,
                            timeout: int = 5) -> dict:
    """Validate a CDP endpoint owns the expected browser before operating.

    Fetches ``/json/version`` and matches ``Browser`` product string against
    ``expect_browser`` (case-insensitive substring on the product name, e.g.
    ``chrome``, ``brave``, ``edge``). Returns the version payload dict.

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


def find_pids_listening_on(port: int, *, proc_root: str = "/proc") -> list[int]:
    """PIDs holding a LISTEN socket on `port` (Linux /proc only).

    Product checks prove *which browser family* answers a port, not
    *which process*. When the workflow launched the browser itself, this
    maps the port back to owner PID(s) so the command line (binary,
    --user-data-dir) can be confirmed. Raises RuntimeError off Linux.
    `proc_root` is injectable for tests.
    """
    port = validate_port(port)
    if os.name != "posix":
        raise RuntimeError("process lookup needs Linux /proc")
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


def read_process_cmdline(pid: int, *, proc_root: str = "/proc") -> str:
    """NUL-joined command line of `pid` (Linux /proc only)."""
    try:
        with open(os.path.join(proc_root, str(int(pid)), "cmdline"),
                  "rb") as fh:
            raw = fh.read()
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read cmdline of pid {pid}: {exc}")
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def verify_endpoint_process(port: int, expect_cmd: str, *,
                            proc_root: str = "/proc") -> int:
    """Confirm a listener on `port` runs a command containing `expect_cmd`.

    Pass a fragment of the launch command this workflow used (binary name
    or `--user-data-dir=…`). Returns the matching PID. Raises ValueError
    when nobody listens there or no owner's command line matches, and
    RuntimeError where process lookup is unsupported. Fail closed.
    """
    want = (expect_cmd or "").strip().lower()
    if not want:
        raise ValueError("verify_endpoint_process needs a non-empty "
                         "expected command fragment")
    pids = find_pids_listening_on(port, proc_root=proc_root)
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
    scratch.mkdir(parents=True, exist_ok=True)
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
