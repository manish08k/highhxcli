"""The model-provider abstraction.

A provider turns a :class:`ModelRequest` (system prompt, neutral messages, tool
specs) into a stream of :mod:`highhx.agent.streaming` events. Adapters exist for
the HighhX platform gateway (the default for Pro), Anthropic, OpenAI and Gemini;
new providers register a factory in :mod:`highhx.agent.model.registry`.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

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


T = TypeVar("T")


class Aborter:
    """Collects ways to abort one in-flight upstream HTTP response (set by the worker as soon
    as the response exists) and runs them from the cancelling thread."""

    def __init__(self) -> None:
        self._actions: list[Callable[[], Any]] = []
        self._lock = threading.Lock()

    def response(self, response: Any) -> None:
        """Register an ``httpx.Response``: its socket is shut down on abort, which wakes a
        thread blocked reading it (closing alone does not)."""

        def shutdown() -> None:
            stream = (getattr(response, "extensions", None) or {}).get("network_stream")
            sock = stream.get_extra_info("socket") if stream is not None else None
            if sock is not None:
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
            with suppress(Exception):
                response.close()

        self.add(shutdown)

    def add(self, action: Callable[[], Any]) -> None:
        with self._lock:
            self._actions.append(action)

    def abort(self) -> None:
        with self._lock:
            actions = list(self._actions)
        for action in actions:
            with suppress(Exception):
                action()


def pump(
    produce: Callable[[Aborter], Iterator[T]], cancel: CancellationToken | None, *, poll: float = 0.05
) -> Iterator[T]:
    """Iterate an SDK stream so that cancellation takes effect immediately.

    ``produce`` runs in a worker thread (SDK streams block in socket reads, which another
    thread cannot interrupt by closing them). The caller waits on a queue it can abandon at
    any time: on cancellation the upstream response is aborted and
    :class:`OperationCancelledError` is raised at once. Exceptions from the SDK are re-raised
    unchanged in the caller, so adapters keep mapping them as before.
    """
    if cancel is None:
        yield from produce(Aborter())
        return
    aborter = Aborter()
    items: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=256)
    stop = threading.Event()

    def put(item: tuple[str, Any]) -> bool:
        while not stop.is_set():
            try:
                items.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def work() -> None:
        try:
            for value in produce(aborter):
                if not put(("item", value)):
                    return
            put(("done", None))
        except BaseException as exc:
            put(("error", exc))

    worker = threading.Thread(target=work, name="model-stream", daemon=True)
    worker.start()
    try:
        while True:
            if cancel.cancelled:
                raise OperationCancelledError("Model response cancelled.")
            try:
                kind, value = items.get(timeout=poll)
            except queue.Empty:
                continue
            if kind == "item":
                yield value
            elif kind == "error":
                if cancel.cancelled:
                    raise OperationCancelledError("Model response cancelled.")
                raise value
            else:
                return
    finally:
        stop.set()
        if worker.is_alive():
            aborter.abort()


def truncated(provider: str) -> ModelProviderError:
    """The upstream stream ended before the response was complete (no final stop signal)."""
    return ModelProviderError(f"The {provider} response ended before it was complete.", retryable=True, status=None)


def transport_error(exc: BaseException, provider: str) -> ModelProviderError | None:
    """Map an HTTP-library exception that escaped the SDK's own error types (SDKs raise raw
    read timeouts and protocol errors from the middle of a stream) to a retryable error.
    Returns None for anything that is not a transport failure."""
    for kind in type(exc).__mro__:
        module = kind.__module__.split(".")[0]
        if module.startswith(("httpx", "httpcore", "h11", "h2")) or kind in (TimeoutError, ConnectionError):
            timeout = any("Timeout" in k.__name__ for k in type(exc).__mro__) or isinstance(exc, TimeoutError)
            message = f"The {provider} API timed out." if timeout else f"The connection to the {provider} API failed."
            return ModelProviderError(message, retryable=True)
    return None
