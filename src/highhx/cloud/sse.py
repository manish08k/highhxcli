"""Server-sent events: the wire format for streamed agent model output.

Used by the HighhX platform gateway (encoding) and the CLI (decoding), so both
ends agree on exactly one format.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ServerEvent:
    event: str
    data: dict[str, Any]
    id: int | None = None
    """Monotonic sequence number within one stream (``id:`` field); used for ordering,
    duplicate suppression and resume via ``Last-Event-ID``."""


def encode(event: str, data: dict[str, Any], *, event_id: int | None = None) -> bytes:
    """One SSE frame. ``data`` is JSON on a single line (JSON never contains raw newlines)."""
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


KEEPALIVE = b": keep-alive\n\n"


def decode(lines: Iterable[bytes | str]) -> Iterator[ServerEvent]:
    """Parse SSE frames from an iterable of lines (bytes or str, with or without newlines)."""
    event = "message"
    data: list[str] = []
    event_id: int | None = None
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if not line:
            if data:
                yield ServerEvent(event, _parse("\n".join(data)), event_id)
            event, data, event_id = "message", [], None
            continue
        if line.startswith(":"):
            continue  # comment / keep-alive
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
        elif name == "id":
            event_id = int(value) if value.isdigit() else None
    if data:
        yield ServerEvent(event, _parse("\n".join(data)), event_id)


def _parse(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    return value if isinstance(value, dict) else {"value": value}
