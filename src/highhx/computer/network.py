"""What the page asked the network for, as evidence for verification ("the order was POSTed and
got 201"), from the DevTools Network domain.

    Network.requestWillBeSent   → method, URL, resource type
    Network.responseReceived    → status
    Network.loadingFailed       → error (blocked, DNS, aborted …)

Privacy by construction: only the method, a *sanitized* URL (scheme, host, port, path, and query
parameter **names** — never their values, never user:password, never the fragment), the resource
type, the status and a failure reason are kept. Headers, cookies and bodies are never read.
The journal is bounded and lives only in the browser object. An action takes the entries that
completed while it ran (``mark`` / ``since``) into its result, where the redactor still applies.
"""

from __future__ import annotations

import itertools
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

MAX_ENTRIES = 500
IGNORED_SCHEMES = ("data", "blob", "chrome", "chrome-extension", "devtools", "about")


def sanitize_url(url: str) -> str:
    """The URL without credentials, fragment or query values: ``https://api.x.test/orders?token=…``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    path = parts.path[:200] or "/"
    query = ""
    if parts.query:
        names = [name for name, _ in parse_qsl(parts.query, keep_blank_values=True)][:20]
        query = "?" + "&".join(f"{n}=…" for n in names) if names else "?…"
    return f"{parts.scheme}://{host}{path}{query}"


@dataclass
class NetworkEntry:
    seq: int
    request_id: str
    method: str
    url: str
    type: str = ""
    status: int | None = None
    error: str = ""
    session: str = ""
    done: int = 0
    """The journal sequence at which the request finished (0: still open)."""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"method": self.method, "url": self.url, "status": self.status, "type": self.type}
        if self.error:
            out["error"] = self.error
        return out


class NetworkJournal:
    def __init__(self, capacity: int = MAX_ENTRIES) -> None:
        self.capacity = capacity
        self._entries: OrderedDict[str, NetworkEntry] = OrderedDict()
        self._seq = itertools.count(1)
        self._last = 0
        self._lock = threading.Lock()

    def handle(self, method: str, params: dict[str, Any], session: str = "") -> None:
        if not method.startswith("Network."):
            return
        request_id = str(params.get("requestId") or "")
        if not request_id:
            return
        with self._lock:
            if method == "Network.requestWillBeSent":
                request = params.get("request") or {}
                url = str(request.get("url") or "")
                if url.split(":", 1)[0].lower() in IGNORED_SCHEMES:
                    return
                key = f"{session}:{request_id}"
                if key in self._entries and params.get("redirectResponse"):
                    # a redirect: the previous hop finished with its redirect status
                    hop = self._entries.pop(key)
                    hop.status = int((params.get("redirectResponse") or {}).get("status") or 0) or None
                    hop.done = self._tick()
                    self._entries[f"{key}#{hop.seq}"] = hop
                self._entries[key] = NetworkEntry(
                    self._tick(),
                    request_id,
                    str(request.get("method") or "GET"),
                    sanitize_url(url),
                    str(params.get("type") or ""),
                    session=session,
                )
            elif method == "Network.responseReceived":
                entry = self._entries.get(f"{session}:{request_id}")
                if entry is not None:
                    entry.status = int((params.get("response") or {}).get("status") or 0) or None
                    entry.type = entry.type or str(params.get("type") or "")
                    entry.done = self._tick()
            elif method == "Network.loadingFailed":
                entry = self._entries.get(f"{session}:{request_id}")
                if entry is not None:
                    entry.error = str(
                        params.get("errorText") or ("blocked" if params.get("blockedReason") else "failed")
                    )[:120]
                    entry.done = self._tick()
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)

    def _tick(self) -> int:
        self._last = next(self._seq)
        return self._last

    def mark(self) -> int:
        with self._lock:
            return self._last

    def since(self, mark: int, *, limit: int = 100) -> list[dict[str, Any]]:
        """Requests that finished (answered or failed) after ``mark``, oldest first."""
        with self._lock:
            done = sorted((e for e in self._entries.values() if e.done > mark), key=lambda e: e.done)
        return [e.to_dict() for e in done[-limit:]]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
