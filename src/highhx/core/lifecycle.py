"""Process lifecycle: signal handling and graceful shutdown."""

from __future__ import annotations

import contextlib
import logging
import signal
import threading
from collections.abc import Callable, Iterator
from types import FrameType

from highhx.execution.cancellation import CancellationToken

log = logging.getLogger(__name__)


class Lifecycle:
    """Owns shutdown hooks and translates termination signals into cancellation."""

    def __init__(self, cancel: CancellationToken) -> None:
        self.cancel = cancel
        self._hooks: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._shut_down = False

    def add_shutdown_hook(self, hook: Callable[[], None]) -> None:
        """Register a callable to run (in reverse order) on shutdown."""
        with self._lock:
            self._hooks.append(hook)

    def shutdown(self) -> None:
        """Run shutdown hooks exactly once; failures are logged."""
        with self._lock:
            if self._shut_down:
                return
            self._shut_down = True
            hooks = list(reversed(self._hooks))
        for hook in hooks:
            try:
                hook()
            except Exception:  # pragma: no cover - defensive
                log.exception("shutdown hook failed")

    def _handler(self, signum: int, _frame: FrameType | None) -> None:
        name = signal.Signals(signum).name
        self.cancel.cancel(f"received {name}")
        # Raising KeyboardInterrupt unwinds blocking waits and prompts in the main
        # thread; subprocess runners catch it and terminate their children.
        raise KeyboardInterrupt(name)

    @contextlib.contextmanager
    def handle_signals(self) -> Iterator[None]:
        """Install SIGINT/SIGTERM handlers for the duration of the block (main thread only)."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        wanted: list[int] = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGBREAK"):
            wanted.append(signal.SIGBREAK)
        previous: dict[int, object] = {}
        for sig in wanted:
            try:
                previous[sig] = signal.signal(sig, self._handler)
            except (ValueError, OSError):  # pragma: no cover - platform specific
                continue
        try:
            yield
        finally:
            for sig, handler in previous.items():
                with contextlib.suppress(ValueError, OSError, TypeError):
                    signal.signal(sig, handler)  # type: ignore[arg-type]
            self.shutdown()
