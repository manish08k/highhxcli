"""CLI ↔ platform protocol versioning (shared by both sides).

Every request carries ``X-HighhX-Protocol: <major>.<minor>`` and
``X-HighhX-Client: highhx-cli/<version>``. The platform accepts a set of
protocol majors and a minimum minor per major; anything else is rejected with
``426 Upgrade Required`` (code ``client_outdated`` or ``client_unsupported``).
Every response carries the platform's own ``X-HighhX-Protocol`` so the CLI can
refuse a platform it does not understand instead of misbehaving silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PROTOCOL_HEADER = "X-HighhX-Protocol"
CLIENT_HEADER = "X-HighhX-Client"
IDEMPOTENCY_HEADER = "Idempotency-Key"

PROTOCOL_VERSION = "1.1"
"""1.0: accounts, gateway, sessions. 1.1: SSE event ids, stream resume, idempotency keys, cancel."""

SUPPORTED_MAJORS = frozenset({1})
MIN_MINOR = {1: 0}

_VERSION = re.compile(r"^(\d{1,3})\.(\d{1,3})$")


@dataclass(frozen=True)
class ProtocolVersion:
    major: int
    minor: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    @property
    def supports_resume(self) -> bool:
        return (self.major, self.minor) >= (1, 1)


def parse(value: str | None) -> ProtocolVersion | None:
    """``"1.1"`` → ProtocolVersion(1, 1); anything malformed → None."""
    if not value:
        return None
    match = _VERSION.match(value.strip())
    if not match:
        return None
    return ProtocolVersion(int(match.group(1)), int(match.group(2)))


def check_client(value: str | None) -> tuple[ProtocolVersion | None, str | None]:
    """Server side: (version, error code) — error is ``client_unsupported`` or ``client_outdated``."""
    version = parse(value)
    if version is None:
        return None, "client_unsupported"
    if version.major not in SUPPORTED_MAJORS:
        return version, "client_unsupported" if version.major > max(SUPPORTED_MAJORS) else "client_outdated"
    if version.minor < MIN_MINOR.get(version.major, 0):
        return version, "client_outdated"
    return version, None


def compatible_server(value: str | None) -> bool:
    """Client side: can this CLI talk to a platform announcing ``value``?"""
    version = parse(value)
    return version is not None and version.major == parse(PROTOCOL_VERSION).major  # type: ignore[union-attr]
