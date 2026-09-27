"""The model-provider abstraction.

A provider turns a :class:`ModelRequest` (system prompt, neutral messages, tool
specs) into a stream of :mod:`highhx.agent.streaming` events. Adapters exist for
the HighhX platform gateway (the default for Pro), Anthropic, OpenAI and Gemini;
new providers register a factory in :mod:`highhx.agent.model.registry`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

from highhx.agent.messages import Message
from highhx.agent.streaming import ModelEvent
from highhx.core.errors import ModelProviderError, OperationCancelledError
from highhx.execution.cancellation import CancellationToken


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the model sees it: a name, what it does, and a JSON Schema for its input."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolSpec:
        params = data.get("parameters")
        return cls(
            str(data["name"]),
            str(data.get("description") or ""),
            params if isinstance(params, dict) else {"type": "object"},
        )


@dataclass
class ModelRequest:
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    model: str | None = None
    """``None`` means the provider's default model."""
    max_tokens: int = 32_000
    effort: str | None = None
    """Reasoning effort hint (low / medium / high / xhigh / max) where the provider supports it."""
    session_id: str | None = None
    attempt_id: str | None = None
    """Idempotency key for this attempt (the platform rejects duplicates and meters each once)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "messages": [m.to_dict() for m in self.messages],
            "tools": [t.to_dict() for t in self.tools],
            "model": self.model,
            "max_tokens": self.max_tokens,
            "effort": self.effort,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelRequest:
        return cls(
            system=str(data.get("system") or ""),
            messages=[Message.from_dict(m) for m in data.get("messages") or []],
            tools=[ToolSpec.from_dict(t) for t in data.get("tools") or []],
            model=str(data["model"]) if data.get("model") else None,
            max_tokens=int(data.get("max_tokens") or 32_000),
            effort=str(data["effort"]) if data.get("effort") else None,
            session_id=str(data["session_id"]) if data.get("session_id") else None,
        )


class ModelProvider(Protocol):
    """Anything that can stream a model response."""

    name: str
    default_model: str

    def stream(self, request: ModelRequest, *, cancel: CancellationToken | None = None) -> Iterator[ModelEvent]:
        """Yield text/tool events as they arrive, ending with exactly one ``Completed``."""
        ...


def parse_tool_arguments(raw: str) -> dict[str, Any]:
    """Parse streamed tool-call arguments strictly; invalid JSON becomes an ``INVALID_JSON`` marker
    that tool validation rejects (the model then gets an error result and can retry)."""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {"INVALID_JSON": raw}
    return value if isinstance(value, dict) else {"INVALID_JSON": raw}


# ---------------------------------------------------------------- adapter helpers
DEFAULT_TIMEOUT = 300.0
"""Seconds of upstream *inactivity* (connect/read) before a provider call fails as retryable."""
DEFAULT_SDK_RETRIES = 2
"""SDK-level retries. They only happen before a response starts streaming (connection
errors, 429, 5xx on the initial request), so no partial output is ever duplicated."""


@contextmanager
def cancellable(cancel: CancellationToken | None, close: Callable[[], Any] | None = None) -> Iterator[None]:
    """Make a blocking SDK stream cancellable: on cancellation ``close`` aborts the in-flight
    HTTP response from the cancelling thread, and whatever error that causes in the reading
    thread is reported as :class:`OperationCancelledError` (never as a provider failure)."""
    if cancel is not None and close is not None:

        def _abort(_reason: str) -> None:
            with suppress(Exception):
                close()

        cancel.on_cancel(_abort)
    try:
        yield
    except OperationCancelledError:
        raise
    except Exception:
        if cancel is not None and cancel.cancelled:
            raise OperationCancelledError("Model response cancelled.") from None
        raise
    if cancel is not None and cancel.cancelled:
        raise OperationCancelledError("Model response cancelled.")


def truncated(provider: str) -> ModelProviderError:
    """The upstream stream ended before the response was complete (no final stop signal)."""
    return ModelProviderError(f"The {provider} response ended before it was complete.", retryable=True, status=None)
