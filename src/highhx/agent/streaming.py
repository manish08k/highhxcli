"""Streaming events emitted by model providers while a response is generated.

Providers yield these as the response arrives; the session forwards text to the
UI immediately and acts on the final :class:`Completed` event. The same events
travel over the platform gateway as server-sent events (see ``to_wire`` /
``from_wire``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from highhx.agent.messages import Message, StopReason, Usage


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    """The model began a tool call (its input is still streaming)."""

    id: str
    name: str


@dataclass(frozen=True)
class Completed:
    """The response finished; ``message`` holds every block in order."""

    message: Message
    stop_reason: StopReason
    usage: Usage
    model: str = ""


ModelEvent = TextDelta | ToolCallStarted | Completed


def to_wire(event: ModelEvent) -> tuple[str, dict[str, Any]]:
    if isinstance(event, TextDelta):
        return "text", {"text": event.text}
    if isinstance(event, ToolCallStarted):
        return "tool_call_started", {"id": event.id, "name": event.name}
    return "completed", {
        "message": event.message.to_dict(),
        "stop_reason": event.stop_reason,
        "usage": event.usage.to_dict(),
        "model": event.model,
    }


def from_wire(name: str, data: dict[str, Any]) -> ModelEvent | None:
    """Inverse of :func:`to_wire`; unknown events return ``None`` (forward compatible)."""
    if name == "text":
        return TextDelta(str(data.get("text") or ""))
    if name == "tool_call_started":
        return ToolCallStarted(str(data.get("id") or ""), str(data.get("name") or ""))
    if name == "completed":
        stop = data.get("stop_reason")
        return Completed(
            Message.from_dict(data.get("message") or {"role": "assistant"}),
            stop if stop in ("end_turn", "tool_use", "max_tokens", "refusal", "pause", "error") else "end_turn",
            Usage.from_dict(data.get("usage")),
            str(data.get("model") or ""),
        )
    return None
