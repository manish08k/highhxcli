"""Time and duration helpers."""

from __future__ import annotations

import re
from datetime import UTC, datetime

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$", re.IGNORECASE)
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def utc_now() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(UTC)


def iso_now() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    return utc_now().isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp produced by :func:`iso_now`."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_duration(value: str | float | None) -> float | None:
    """Parse ``"30s"``, ``"5m"``, ``"1.5h"``, ``250ms`` or a number of seconds.

    Returns None for None. Raises ValueError on malformed input.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid duration: {value!r}")
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError("duration must not be negative")
        return float(value)
    match = _DURATION_RE.match(str(value))
    if not match:
        raise ValueError(f"invalid duration: {value!r} (expected e.g. 30s, 5m, 1h)")
    amount = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    return amount * _UNITS[unit]


def format_duration(seconds: float | None) -> str:
    """Human readable duration: ``850ms``, ``4.2s``, ``3m 05s``, ``1h 02m``."""
    if seconds is None:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def humanize_ago(value: str | datetime | None, now: datetime | None = None) -> str:
    """Relative description such as ``5m ago``."""
    if value is None:
        return "never"
    moment = parse_iso(value) if isinstance(value, str) else value
    delta = ((now or utc_now()) - moment).total_seconds()
    if delta < 60:
        return f"{int(max(delta, 0))}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"
