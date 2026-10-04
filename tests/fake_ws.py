"""Minimal RFC 6455 server for testing the CDP WebSocket client.

Implements only what the client needs: the handshake, masked client frames,
unmasked server frames, ping/pong. Enough to exercise the real socket path
rather than mocking it away.
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeWS:
    """Serve one or more connections, replying with `reply_for(message)`."""

    def __init__(self, reply_for, *, reject_handshake=False, bad_accept=False,
                 drop_after_handshake=False, never_reply=False):
        self.reply_for = reply_for
        self.reject_handshake = reject_handshake
        self.bad_accept = bad_accept
        self.drop_after_handshake = drop_after_handshake
        self.never_reply = never_reply
        self.received = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/devtools/page/X"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(timeout=5)

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                self._handle(conn)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        head = buf.split(b"\r\n\r\n")[0].decode("latin-1")
        key = ""
        for line in head.split("\r\n")[1:]:
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        if self.reject_handshake:
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + GUID).encode()).digest()).decode()
        if self.bad_accept:
            accept = "AAAAAAAAAAAAAAAAAAAAAAAAAAA="
        conn.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        if self.drop_after_handshake:
            return
        conn.settimeout(10)
        while True:
            msg = self._read_client_frame(conn)
            if msg is None:
                return
            if self.never_reply:
                return
            self.received.append(msg)
            out = json.dumps(self.reply_for(msg)).encode()
            self._send_server_frame(conn, out)

    def _read_client_frame(self, conn):
        def rd(n):
            b = b""
            while len(b) < n:
                c = conn.recv(n - len(b))
                if not c:
                    return None
                b += c
            return b
        head = rd(2)
        if not head:
            return None
        b0, b1 = head
        masked = bool(b1 & 0x80)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", rd(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", rd(8))[0]
        key = rd(4) if masked else b""
        payload = rd(n) if n else b""
        if payload is None:
            return None
        if masked:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return json.loads(payload.decode())

    @staticmethod
    def _send_server_frame(conn, payload: bytes):
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        conn.sendall(bytes(header) + payload)   # servers do not mask
