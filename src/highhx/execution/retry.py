"""Retry policies with exponential backoff."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from highhx.utils.time import parse_duration


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to attempt an operation and how long to wait in between.

    ``attempts`` counts the total number of tries (1 = no retry). The delay
    before retry *n* (1-based) is ``delay * backoff**(n-1)`` capped at ``max_delay``.
    """

    attempts: int = 1
    delay: float = 0.0
    backoff: float = 2.0
    max_delay: float = 300.0
    retry_on_timeout: bool = True

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("retry attempts must be >= 1")
        if self.delay < 0 or self.max_delay < 0:
            raise ValueError("retry delays must not be negative")
        if self.backoff < 1:
            raise ValueError("retry backoff must be >= 1")

    def delay_before(self, retry_number: int) -> float:
        """Delay before the ``retry_number``-th retry (1-based)."""
        if retry_number < 1:
            return 0.0
        return min(self.delay * (self.backoff ** (retry_number - 1)), self.max_delay)

    def delays(self) -> Iterator[float]:
        """Yield the delay before each retry (``attempts - 1`` values)."""
        for number in range(1, self.attempts):
            yield self.delay_before(number)

    @classmethod
    def from_value(cls, value: Any) -> RetryPolicy:
        """Build from config: an int (attempts) or a mapping."""
        if value is None:
            return cls()
        if isinstance(value, int) and not isinstance(value, bool):
            return cls(attempts=value, delay=1.0)
        if isinstance(value, dict):
            return cls(
                attempts=int(value.get("attempts", 1)),
                delay=parse_duration(value.get("delay", 0)) or 0.0,
                backoff=float(value.get("backoff", 2.0)),
                max_delay=parse_duration(value.get("max_delay", 300)) or 300.0,
                retry_on_timeout=bool(value.get("retry_on_timeout", True)),
            )
        raise ValueError(f"invalid retry configuration: {value!r}")
