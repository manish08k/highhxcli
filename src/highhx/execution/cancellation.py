"""Cooperative cancellation tokens.

A token can be cancelled once; waiters wake up and registered callbacks run.
Child tokens are cancelled automatically when their parent is cancelled, which
lets a workflow cancel all running steps (fail-fast, Ctrl+C) in one call.
"""

from __future__ import annotations

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
            callbacks = list(self._callbacks)
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
        """Create a token cancelled whenever this one is."""
        child = CancellationToken()
        self.on_cancel(child.cancel)
        return child
