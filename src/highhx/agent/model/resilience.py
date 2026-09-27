"""Retry, backoff and circuit breaking for model calls.

Only *model calls* are retried — they are idempotent from the project's point of
view (nothing has executed yet). Tool executions are never retried automatically.
Retries use exponential backoff with jitter and a hard attempt limit; repeated
failures open a circuit so the session fails fast instead of hammering a provider
that is down (deterministic HighhX commands keep working regardless).
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from highhx.core.errors import ModelProviderError


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    """Total attempts including the first."""
    base_delay: float = 1.0
    max_delay: float = 20.0
    jitter: float = 0.25
    """Fraction of the delay randomised (avoids synchronised retries)."""

    def delay(self, attempt: int, rng: Callable[[], float] = random.random) -> float:
        """Delay before retry number ``attempt`` (1-based)."""
        raw = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return max(0.0, raw * (1 - self.jitter + 2 * self.jitter * rng()))


@dataclass
class CircuitBreaker:
    failure_threshold: int = 4
    reset_after: float = 30.0
    clock: Callable[[], float] = time.monotonic
    failures: int = 0
    opened_at: float | None = field(default=None)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def open(self) -> bool:
        # After ``reset_after`` the circuit is half-open: one trial call is allowed.
        return self.opened_at is not None and self.clock() - self.opened_at < self.reset_after

    def check(self) -> None:
        if self.open:
            remaining = self.reset_after - (self.clock() - (self.opened_at or 0))
            raise ModelProviderError(
                "The AI provider is failing repeatedly; pausing requests.",
                hint=f"Try again in {remaining:.0f}s. Deterministic HighhX commands (test, check, build …) still work.",
                retryable=False,
            )

    def success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.opened_at = self.clock()
