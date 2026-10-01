"""A minimal RFC 6455 WebSocket client (text frames, ping/pong, close) for DevTools connections —
local, or a configured remote browser over TLS. Standard library only; every blocking read
observes a cancellation token."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import time
from urllib.parse import urlparse

from highhx.core.errors import IntegrationError, OperationCancelledError
from highhx.execution.cancellation import CancellationToken

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 64 * 1024 * 1024


class WebSocketClosed(IntegrationError):
    """The connection was closed."""


class WebSocketTimeout(IntegrationError):
    """No complete message arrived before the deadline. If part of a frame had been read the
    connection is closed (the stream would be unusable); between messages it stays open —
    ``closed`` tells which."""


LOOPBACK = ("127.0.0.1", "localhost", "::1")


class WebSocket:
    """``ws://`` to this computer (the HighhX browser, or an ``ssh -L`` tunnel), or — only when a
    remote browser was configured explicitly (``allow_remote``) — ``wss://`` with verified TLS.
    Plain ``ws://`` to another host is refused: DevTools controls the browser completely."""

    def __init__(self, url: str, *, timeout: float = 30.0, allow_remote: bool = False) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise IntegrationError(f"Not a WebSocket DevTools endpoint: {url.split('?')[0]!r}")
        host = parsed.hostname or "127.0.0.1"
        local = host in LOOPBACK
        if not local and not allow_remote:
            raise IntegrationError("Refusing to connect to a non-local DevTools endpoint.")
        if not local and parsed.scheme != "wss":
            raise IntegrationError(
                "A remote browser's DevTools must use wss:// (TLS); for plain ws:// use an SSH tunnel to 127.0.0.1."
            )
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        if parsed.scheme == "wss":
            import ssl

            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IntegrationError("DevTools closed the connection during the handshake.")
            response += chunk
        head, _, self._buffer = response.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if not lines[0].startswith("HTTP/1.1 101"):
            raise IntegrationError(f"WebSocket handshake failed: {lines[0]}")
        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode(), usedforsecurity=False).digest()).decode()
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
        if headers.get("sec-websocket-accept") != accept:
            raise IntegrationError("WebSocket handshake failed: bad Sec-WebSocket-Accept.")
        self.closed = False

    # --------------------------------------------------------------- framing
    def send(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise WebSocketClosed("The DevTools connection is closed.")
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self.sock.sendall(header + mask + masked)
        except OSError:  # broken pipe / reset: the browser went away
            self._abort()
            raise WebSocketClosed("The DevTools connection was lost while sending.") from None

    def _read_exact(
        self,
        n: int,
        cancel: CancellationToken | None,
        deadline_poll: float,
        deadline: float | None = None,
        *,
        boundary: bool = False,
    ) -> bytes:
        """``boundary``: nothing of the current message has been read, so giving up (cancel,
        deadline) leaves the stream intact and the connection open."""
        while len(self._buffer) < n:
            intact = boundary and not self._buffer
            if cancel is not None and cancel.cancelled:
                if not intact:
                    self.close()  # a half-read frame leaves the stream unusable
                raise OperationCancelledError("Browser operation cancelled.")
            if deadline is not None and time.monotonic() > deadline:
                if not intact:
                    self.close()
                raise WebSocketTimeout("No answer from the browser in time.")
            self.sock.settimeout(deadline_poll)
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                continue
            except OSError:  # reset / aborted: the browser crashed or was killed
                self._abort()
                raise WebSocketClosed("The DevTools connection was reset.") from None
            if not chunk:
                self._abort()
                raise WebSocketClosed("The browser closed the DevTools connection.")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def recv(self, *, cancel: CancellationToken | None = None, poll: float = 0.2, deadline: float | None = None) -> str:
        """Next complete text message (control frames are handled transparently).

        ``deadline`` is a ``time.monotonic()`` value; past it :class:`WebSocketTimeout` is raised."""
        message = b""
        while True:
            b1, b2 = self._read_exact(2, cancel, poll, deadline, boundary=not message)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2, cancel, poll, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8, cancel, poll, deadline))[0]
            if length > MAX_MESSAGE:
                raise IntegrationError("DevTools message too large.")
            mask = self._read_exact(4, cancel, poll, deadline) if b2 & 0x80 else b""
            payload = self._read_exact(length, cancel, poll, deadline) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                self.close()
                raise WebSocketClosed("The browser closed the DevTools connection.")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if fin:
                return message.decode("utf-8", errors="replace")

    def _abort(self) -> None:
        """The peer is gone: release the socket without a closing handshake."""
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._send_frame(0x8, b"")
        except (OSError, WebSocketClosed):
            pass
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
