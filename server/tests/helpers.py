"""Helpers shared by the platform tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from highhx.agent.messages import Message, TextBlock, ToolCall, Usage
from highhx.agent.model.base import ModelRequest
from highhx.agent.streaming import Completed, ModelEvent, TextDelta
from highhx.execution.cancellation import CancellationToken

PASSWORD = "correct horse battery"


@dataclass
class Upstream:
    """Scripted upstream provider shared by every request (records what the gateway sent)."""

    responses: list[list[ModelEvent] | Exception] = field(default_factory=list)
    requests: list[tuple[str, ModelRequest]] = field(default_factory=list)

    def factory(self, name: str, key: str) -> Any:
        upstream = self

        class _Provider:
            default_model = "x"

            def __init__(self) -> None:
                self.name = name

            def stream(self, request: ModelRequest, *, cancel: CancellationToken | None = None) -> Iterator[ModelEvent]:
                upstream.requests.append((name, request))
                step = upstream.responses.pop(0) if upstream.responses else text_reply("ok")
                if isinstance(step, Exception):
                    raise step
                yield from step

        return _Provider()


def text_reply(text: str, *, usage: Usage | None = None, model: str = "claude-opus-5") -> list[ModelEvent]:
    return [
        TextDelta(text),
        Completed(Message("assistant", [TextBlock(text)]), "end_turn", usage or Usage(1000, 200), model),
    ]


def tool_reply(name: str, args: dict[str, Any], call_id: str = "call_1") -> list[ModelEvent]:
    return [
        Completed(Message("assistant", [ToolCall(call_id, name, args)]), "tool_use", Usage(500, 50), "claude-opus-5")
    ]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class GatedUpstream:
    """An upstream that emits events one at a time when released, honours cancellation and
    records every call (to prove the model is called once per idempotency key)."""

    def __init__(self, events: list[ModelEvent]) -> None:
        import threading

        self.events = events
        self.calls = 0
        self.gates = [threading.Event() for _ in events]
        self.cancelled = threading.Event()
        self.finished = threading.Event()

    def release(self, count: int | None = None) -> None:
        for gate in self.gates[: count if count is not None else len(self.gates)]:
            gate.set()

    def factory(self, name: str, key: str) -> Any:
        upstream = self

        class _Provider:
            default_model = "x"

            def __init__(self) -> None:
                self.name = name

            def stream(self, request: ModelRequest, *, cancel: CancellationToken | None = None) -> Iterator[ModelEvent]:
                upstream.calls += 1
                try:
                    for gate, event in zip(upstream.gates, upstream.events, strict=True):
                        while not gate.wait(0.02):
                            if cancel is not None and cancel.cancelled:
                                upstream.cancelled.set()
                                return
                        yield event
                finally:
                    upstream.finished.set()

        return _Provider()


class LiveServer:
    """The platform served by a real uvicorn on a free local port (real sockets, real disconnects)."""

    def __init__(self, app: Any) -> None:
        import socket
        import threading

        import uvicorn

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> LiveServer:
        import time

        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.server.started
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def json(
        self, method: str, path: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        import http.client
        import json as _json

        from highhx.cloud.protocol import PROTOCOL_HEADER, PROTOCOL_VERSION

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(
            method,
            path,
            body=_json.dumps(body) if body is not None else None,
            headers={"Content-Type": "application/json", PROTOCOL_HEADER: PROTOCOL_VERSION, **(headers or {})},
        )
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, _json.loads(data) if data else {}

    def stream(self, body: dict[str, Any], headers: dict[str, str], limit: int | None = None) -> list[Any]:
        """Read up to ``limit`` SSE events, then drop the TCP connection abruptly."""
        import http.client
        import json as _json
        import socket

        from highhx.cloud import sse
        from highhx.cloud.protocol import PROTOCOL_HEADER, PROTOCOL_VERSION

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(
            "POST",
            "/v1/ai/messages",
            body=_json.dumps(body),
            # The events are read from the raw socket until EOF: don't keep the connection alive.
            headers={
                "Content-Type": "application/json",
                "Connection": "close",
                PROTOCOL_HEADER: PROTOCOL_VERSION,
                **headers,
            },
        )
        response = conn.getresponse()
        if response.status != 200:
            payload = response.read()
            conn.close()
            raise AssertionError(f"HTTP {response.status}: {payload[:300]!r}")
        events = []
        try:
            for event in sse.decode(iter(response.fp.readline, b"")):
                events.append(event)
                if limit is not None and len(events) >= limit:
                    break
        finally:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass
            conn.close()
        return events
