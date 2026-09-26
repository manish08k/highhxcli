"""Deadline tracking."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Deadline:
    """A point in monotonic time after which an operation has timed out."""

    expires_at: float | None

    @classmethod
    def after(cls, seconds: float | None) -> Deadline:
        """Deadline ``seconds`` from now (None = never)."""
        if seconds is None or seconds <= 0:
            return cls(None)
        return cls(time.monotonic() + seconds)

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and time.monotonic() >= self.expires_at

    def remaining(self) -> float | None:
        """Seconds remaining (never negative), or None if unbounded."""
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - time.monotonic())
