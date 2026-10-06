"""Cooperative cancellation tokens.

A token can be cancelled once; waiters wake up and registered callbacks run.
Child tokens are cancelled automatically when their parent is cancelled, which
lets a workflow cancel all running steps (fail-fast, Ctrl+C) in one call.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)


class CancellationToken:
    """Thread-safe cancellation signal."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[str], None]] = []
        self._parent: CancellationToken | None = None
        self.reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "cancelled") -> None:
        """Cancel the token (idempotent) and invoke callbacks."""
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason
            self._event.set()
            callbacks, self._callbacks = list(self._callbacks), []  # each runs once; none is kept
        for callback in callbacks:
            try:
                callback(reason)
            except Exception:  # pragma: no cover - defensive
                log.exception("cancellation callback failed")

    def on_cancel(self, callback: Callable[[str], None]) -> None:
        """Register ``callback``; runs immediately if already cancelled."""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
        callback(self.reason or "cancelled")

    def wait(self, timeout: float | None = None) -> bool:
        """Block up to ``timeout`` seconds; returns True if cancelled."""
        return self._event.wait(timeout)

    def child(self) -> CancellationToken:
        """Create a token cancelled whenever this one is. Call :meth:`detach` on it when its work is
        over: until then this token keeps it (an executor makes one per action — kept forever, a
        long session held every action it ever ran)."""
        child = CancellationToken()
        child._parent = self
        self.on_cancel(child.cancel)
        return child

    def detach(self) -> None:
        """Stop following the parent (the work this token guarded is over)."""
        parent, self._parent = self._parent, None
        if parent is not None:
            with parent._lock, contextlib.suppress(ValueError):
                parent._callbacks.remove(self.cancel)
