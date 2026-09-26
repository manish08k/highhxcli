"""Event subscribers that persist runtime events."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from highhx.core.events import (
    APPROVAL_DECIDED,
    COMMAND_RETRY,
    STEP_FINISHED,
    STEP_STARTED,
    Event,
    EventBus,
)
from highhx.storage.logs import LogWriter


def _describe(event: Event) -> str | None:
    data: dict[str, Any] = event.data
    if event.name == COMMAND_RETRY:
        return (
            f"retrying `{data.get('command')}` after attempt {data.get('attempt')} failed "
            f"(exit {data.get('exit_code')}); waiting {data.get('delay', 0):.1f}s"
        )
    if event.name == STEP_STARTED:
        return f"step {data.get('step')} started"
    if event.name == STEP_FINISHED:
        return f"step {data.get('step')} finished: {data.get('status')}"
    if event.name == APPROVAL_DECIDED and data.get("mode") != "auto":
        verdict = "approved" if data.get("approved") else "denied"
        return f"approval {verdict} ({data.get('mode')}): {data.get('action')}"
    return None


def bridge_to_log(bus: EventBus, writer_for: Callable[[], LogWriter | None]) -> Callable[[], None]:
    """Write notable events into the active execution log. Returns an unsubscribe function."""

    def _handler(event: Event) -> None:
        text = _describe(event)
        writer = writer_for()
        if text and writer is not None:
            writer.event(text)

    return bus.subscribe("*", _handler)
