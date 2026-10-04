"""Dirty-form guard: refuse to close a tab holding unsaved user input.

The single loss a session backup cannot restore is work in progress -- a
half-filled form, an unsent comment, a renamed file. Closing such a tab
destroys it silently, and no amount of enumerate-then-verify catches it,
because the state lives inside the page rather than in the URL.

This talks CDP over a WebSocket because `Runtime.evaluate` has no HTTP
equivalent. The client here is deliberately minimal and dependency-free:
this skill must run on a bare Python install, so a third-party websocket
package is not an option.

Safety posture: FAILS CLOSED. If the probe cannot reach a definitive answer
-- no websocket URL, refused handshake, timeout, unparseable reply -- the
tab is reported as dirty and skipped. A guard that silently passes when it
cannot see is worse than no guard.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
from urllib.parse import urlparse

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_FRAME = 8 * 1024 * 1024

# Evaluated in the page. Deliberately conservative: it reports any non-empty
# editable field, including a single stray character, because leaving a tab
# open costs far less than losing typed work.
DIRTY_FORM_JS = r"""
(() => {
  const parts = [];
  const dirty = (el) => {
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (['checkbox', 'radio', 'file', 'submit', 'button', 'image',
            'reset', 'hidden'].includes(t)) return null;
      return el.value && String(el.value).trim() ? 'input.' + t : null;
    }
    if (tag === 'textarea') {
      return el.value && String(el.value).trim() ? 'textarea' : null;
    }
    if (el.isContentEditable) {
      return el.innerText && el.innerText.trim() ? 'contenteditable' : null;
    }
    return null;
  };
  for (const el of document.querySelectorAll('input, textarea, [contenteditable]')) {
    const why = dirty(el);
    if (why) parts.push(why);
    if (parts.length >= 5) break;
  }
  const files = document.querySelectorAll('input[type=file]');
  for (const f of files) { if (f.files && f.files.length) parts.push('input.file'); }
  // readyState is reported so the caller can refuse to conclude "clean" from a
  // half-parsed DOM. `loading` means the document is still being built and a
  // form may not exist yet; `interactive` means the DOM is complete and
  // subresources are still arriving, so the field scan above IS reliable.
  return { dirty: parts.length > 0, fields: parts, readyState: document.readyState };
})()
"""


class WebSocketError(Exception):
    """Any failure to speak WebSocket. Always fails the guard closed."""


def _read_until(sock: socket.socket, sep: bytes, limit: int = 65536) -> bytes:
    buf = b""
    while sep not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise WebSocketError("connection closed during handshake")
        buf += chunk
        if len(buf) > limit:
            raise WebSocketError("handshake response too large")
    return buf


def ws_connect(url: str, timeout: float = 5.0) -> socket.socket:
    """Open a WebSocket and complete the RFC 6455 handshake."""
    u = urlparse(url)
    if u.scheme != "ws":
        raise WebSocketError(f"unsupported scheme {u.scheme!r}")
    host = u.hostname or "127.0.0.1"
    port = u.port or 80
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock.sendall((
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii"))
        head = _read_until(sock, b"\r\n\r\n").decode("latin-1")
        if " 101" not in head.split("\r\n")[0]:
            raise WebSocketError(
                f"handshake rejected: {head.splitlines()[0][:80]}")
        # Verify the accept token: without this a plain HTTP server could
        # answer 101 and we would believe we have a WebSocket.
        # SHA-1 is not a choice here: RFC 6455 s4.2.2 step 5 defines
        # accept = base64(SHA1(key + GUID)) and the server computes it the
        # same way, so using anything stronger would break every handshake.
        # Not used for integrity or signing - it only proves the peer is a
        # WebSocket endpoint rather than a plain HTTP server.
        # nosemgrep: python.security.hashlib.insecure-hash-algorithm
        expect = base64.b64encode(hashlib.sha1(  # noqa: S324
            (key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        got = ""
        for line in head.split("\r\n")[1:]:
            if line.lower().startswith("sec-websocket-accept:"):
                got = line.split(":", 1)[1].strip()
        if got != expect:
            raise WebSocketError("bad Sec-WebSocket-Accept")
    except Exception:
        sock.close()
        raise
    sock.settimeout(timeout)
    return sock


def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    if len(payload) > MAX_FRAME:
        raise WebSocketError("payload too large")
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)          # clients MUST mask
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    key = os.urandom(4)
    header += key
    sock.sendall(bytes(header)
                 + bytes(b ^ key[i % 4] for i, b in enumerate(payload)))


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WebSocketError("connection closed mid-frame")
        buf += chunk
    return buf


def _recv_frame(sock: socket.socket) -> tuple[int, bool, bytes]:
    b0, b1 = _recv_exact(sock, 2)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    if n > MAX_FRAME:
        raise WebSocketError("frame too large")
    key = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, n) if n else b""
    if masked:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return opcode, fin, payload


def ws_send_text(sock: socket.socket, text: str) -> None:
    _send_frame(sock, OP_TEXT, text.encode("utf-8"))


def ws_recv_text(sock: socket.socket) -> str:
    """Read one complete text message, handling ping/pong and continuations."""
    parts: list[bytes] = []
    while True:
        opcode, fin, payload = _recv_frame(sock)
        if opcode == OP_PING:
            _send_frame(sock, OP_PONG, payload)
            continue
        if opcode == OP_PONG:
            continue
        if opcode == OP_CLOSE:
            raise WebSocketError("peer closed the connection")
        if opcode == OP_BINARY:
            raise WebSocketError("unexpected binary frame")
        parts.append(payload)
        if fin:
            try:
                return b"".join(parts).decode("utf-8")
            except UnicodeDecodeError:
                raise WebSocketError("text frame was not valid UTF-8")


def evaluate(sock: socket.socket, expression: str, msg_id: int = 1) -> dict:
    """One Runtime.evaluate round-trip; returns the parsed CDP message."""
    ws_send_text(sock, json.dumps({
        "id": msg_id,
        "method": "Runtime.evaluate",
        "params": {"expression": expression, "returnByValue": True,
                   "awaitPromise": False},
    }))
    raw = ws_recv_text(sock)
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WebSocketError(f"non-JSON reply: {exc}") from exc
    if not isinstance(msg, dict):
        raise WebSocketError("unexpected reply shape")
    if "error" in msg:
        raise WebSocketError(f"CDP error: {str(msg['error'])[:80]}")
    return msg


def probe_dirty_form(ws_url: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    """(is_dirty, reason) for one tab. Never raises.

    Fails closed: anything preventing a definitive answer returns is_dirty=True,
    so the caller skips the close rather than destroying unsaved work on a
    guess.
    """
    if not ws_url or not str(ws_url).startswith("ws://"):
        # Name the remedy, not just the symptom. An operator who sees this and
        # is not told what to do is simply stuck, and the obvious wrong move is
        # to assume the tab was clean.
        return True, ("no-websocket-url: this browser does not publish one, "
                      "so every close is blocked; re-run cdp_close.py with "
                      "--no-form-guard to accept that risk")
    sock = None
    try:
        sock = ws_connect(str(ws_url), timeout=timeout)
        msg = evaluate(sock, DIRTY_FORM_JS)
        value = ((msg.get("result") or {}).get("result") or {}).get("value")
        if not isinstance(value, dict):
            return True, "unparseable Runtime.evaluate result (failing closed)"
        ready = value.get("readyState")
        if ready == "loading":
            # Found by testing against a real browser: a page still parsing
            # has no inputs yet, so the scan returns an empty result that is
            # indistinguishable from a genuinely empty form. "No fields" on a
            # half-built DOM proves nothing, so refuse to conclude clean.
            return True, "page-still-loading (cannot verify)"
        if value.get("dirty"):
            fields = [str(x) for x in (value.get("fields") or [])][:3]
            return True, "unsaved-input:" + ",".join(fields)
        return False, f"clean (readyState={ready})"
    except Exception as exc:  # noqa: BLE001 - failing closed is the point
        return True, f"probe-failed:{type(exc).__name__}:{str(exc)[:60]}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
