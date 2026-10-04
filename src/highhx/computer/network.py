"""What the page asked the network for, as evidence for verification ("the order was POSTed and
got 201"), from the DevTools Network domain.

    Network.requestWillBeSent   → method, URL, resource type (a redirect closes the previous hop)
    Network.responseReceived    → status
    Network.loadingFinished     → duration (DevTools timestamps, first byte sent → body received)
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
import time
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
    sent: float | None = None
    """DevTools' monotonic timestamp (seconds) of the request; ``ended`` of its last event."""
    ended: float | None = None
    redirect: bool = False
    """This hop was answered with a redirect (the next hop has the same request id)."""
    finished: float = 0.0
    """``time.monotonic()`` when HighhX saw it finish (for "finished in the last 2 seconds")."""

    @property
    def duration_ms(self) -> int | None:
        if self.sent is None or self.ended is None or self.ended < self.sent:
            return None
        return round((self.ended - self.sent) * 1000)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"method": self.method, "url": self.url, "status": self.status, "type": self.type}
        if self.error:
            out["error"] = self.error
        if self.duration_ms is not None:
            out["ms"] = self.duration_ms
        if self.redirect:
            out["redirect"] = True
        return out

    def matches(self, *, url_contains: str = "", method: str = "", status: int | None = None) -> bool:
        return (
            (not url_contains or url_contains in self.url)
            and (not method or self.method.upper() == method.upper())
            and (status is None or self.status == status)
        )


def _stamp(params: dict[str, Any]) -> float | None:
    value = params.get("timestamp")
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


class NetworkJournal:
    def __init__(self, capacity: int = MAX_ENTRIES) -> None:
        self.capacity = capacity
        self._entries: OrderedDict[str, NetworkEntry] = OrderedDict()
        self._seq = itertools.count(1)
        self._last = 0
        self._lock = threading.Lock()
        self.last_activity = time.monotonic()
        """When the journal last saw a request start or finish (for "the network is idle")."""

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
                    hop.redirect, hop.ended = True, _stamp(params)
                    hop.done, hop.finished = self._tick(), time.monotonic()
                    self._entries[f"{key}#{hop.seq}"] = hop
                self._entries[key] = NetworkEntry(
                    self._tick(),
                    request_id,
                    str(request.get("method") or "GET"),
                    sanitize_url(url),
                    str(params.get("type") or ""),
                    session=session,
                    sent=_stamp(params),
                )
            elif method == "Network.responseReceived":
                entry = self._entries.get(f"{session}:{request_id}")
                if entry is not None:
                    entry.status = int((params.get("response") or {}).get("status") or 0) or None
                    entry.type = entry.type or str(params.get("type") or "")
                    entry.ended = _stamp(params) or entry.ended
                    entry.done, entry.finished = self._tick(), time.monotonic()
            elif method == "Network.loadingFinished":
                entry = self._entries.get(f"{session}:{request_id}")
                if entry is not None:
                    entry.ended = _stamp(params) or entry.ended
                    if not entry.done:
                        entry.done, entry.finished = self._tick(), time.monotonic()
            elif method == "Network.loadingFailed":
                entry = self._entries.get(f"{session}:{request_id}")
                if entry is not None:
                    entry.error = str(
                        params.get("errorText") or ("blocked" if params.get("blockedReason") else "failed")
                    )[:120]
                    entry.ended = _stamp(params) or entry.ended
                    entry.done, entry.finished = self._tick(), time.monotonic()
            else:
                return
            self.last_activity = time.monotonic()
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

    def inflight(self) -> int:
        """Requests seen starting that have neither been answered nor failed."""
        with self._lock:
            return sum(1 for e in self._entries.values() if not e.done)

    def idle_for(self) -> float:
        """Seconds since the last request started or finished."""
        return time.monotonic() - self.last_activity

    def find(
        self,
        mark: int,
        *,
        url_contains: str = "",
        method: str = "",
        status: int | None = None,
        finished_after: float | None = None,
    ) -> dict[str, Any] | None:
        """The first request that finished after ``mark`` (or, with ``finished_after``, after that
        ``time.monotonic()``) and matches the sanitized URL, method and status."""
        with self._lock:
            done = sorted(
                (
                    e
                    for e in self._entries.values()
                    if e.done and (e.done > mark if finished_after is None else e.finished >= finished_after)
                ),
                key=lambda e: e.done,
            )
        for entry in done:
            if entry.matches(url_contains=url_contains, method=method, status=status):
                return entry.to_dict()
        return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
