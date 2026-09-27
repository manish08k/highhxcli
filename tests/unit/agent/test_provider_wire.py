"""Provider adapters driven through the *real* vendor SDKs over HTTP.

A local server speaks each vendor's streaming wire format (Anthropic Messages SSE,
OpenAI Chat Completions SSE, Gemini streamGenerateContent SSE), so the official SDKs
parse real bytes: streaming, tool calls, usage, HTTP errors, truncated streams,
timeouts and cancellation. This is MOCKED INTEGRATION — it proves the adapters and the
SDKs agree on the protocol, not that a vendor account works (see the opt-in live tests).
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from highhx.agent.messages import Message
from highhx.agent.model.base import ModelRequest, ToolSpec
from highhx.agent.streaming import Completed, TextDelta, ToolCallStarted
from highhx.core.errors import ModelProviderError, OperationCancelledError
from highhx.execution.cancellation import CancellationToken

pytest.importorskip("anthropic")
pytest.importorskip("openai")
pytest.importorskip("google.genai")


@dataclass
class Reply:
    status: int = 200
    frames: list[str] = field(default_factory=list)
    json_body: dict[str, Any] | None = None
    stall_after: int | None = None
    """Stop sending (without closing) after this many frames."""
    cut_after: int | None = None
    """Close the connection after this many frames (a truncated stream)."""


class WireServer:
    def __init__(self) -> None:
        self.replies: list[Reply] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.release = threading.Event()
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                server.requests.append((self.path, body))
                reply = server.replies.pop(0) if server.replies else Reply(500, json_body={"error": "no script"})
                if reply.json_body is not None:
                    data = json.dumps(reply.json_body).encode()
                    self.send_response(reply.status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(reply.status)
                self.send_header("content-type", "text/event-stream")
                self.send_header("connection", "close")
                self.end_headers()
                for index, frame in enumerate(reply.frames):
                    if reply.cut_after is not None and index >= reply.cut_after:
                        break
                    if reply.stall_after is not None and index >= reply.stall_after:
                        server.release.wait(30)
                        return
                    try:
                        self.wfile.write(frame.encode())
                        self.wfile.flush()
                    except OSError:
                        return
                self.close_connection = True

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[WireServer]:
    wire = WireServer()
    yield wire
    wire.close()


def sse(event: str | None, data: dict[str, Any] | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data)
    return (f"event: {event}\n" if event else "") + f"data: {payload}\n\n"


def request(*, tools: bool = False, model: str | None = None) -> ModelRequest:
    specs = [ToolSpec("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})]
    return ModelRequest("system", [Message.user("hello")], specs if tools else [], model=model)


# ------------------------------------------------------------------ wire formats
def anthropic_frames(*, tool: bool = False) -> list[str]:
    frames = [
        sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 12, "output_tokens": 1},
                },
            },
        ),
        sse(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        ),
        sse(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
        ),
        sse(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}},
        ),
        sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ]
    if tool:
        frames += [
            sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}},
                },
            ),
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path": "a.py"}'},
                },
            ),
            sse("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ]
    frames += [
        sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 7},
            },
        ),
        sse("message_stop", {"type": "message_stop"}),
    ]
    return frames


def openai_frames(*, tool: bool = False) -> list[str]:
    def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        return sse(
            None,
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "gpt-5",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            },
        )

    frames = [chunk({"role": "assistant", "content": "Hello"}), chunk({"content": " world"})]
    if tool:
        frames.append(
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                        }
                    ]
                }
            )
        )
    frames.append(chunk({}, "tool_calls" if tool else "stop"))
    frames.append(
        sse(
            None,
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "gpt-5",
                "choices": [],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
            },
        )
    )
    frames.append("data: [DONE]\n\n")
    return frames


def gemini_frames(*, tool: bool = False) -> list[str]:
    parts: list[dict[str, Any]] = [{"text": " world"}]
    if tool:
        parts.append({"functionCall": {"name": "read_file", "args": {"path": "a.py"}}})
    return [
        sse(None, {"candidates": [{"content": {"role": "model", "parts": [{"text": "Hello"}]}, "index": 0}]}),
        sse(
            None,
            {
                "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 7, "totalTokenCount": 19},
            },
        ),
    ]


def error_body(provider: str, status: int) -> dict[str, Any]:
    if provider == "anthropic":
        kind = {401: "authentication_error", 429: "rate_limit_error"}.get(status, "api_error")
        return {"type": "error", "error": {"type": kind, "message": f"status {status}"}}
    if provider == "openai":
        return {"error": {"message": f"status {status}", "type": "error", "code": str(status)}}
    return {"error": {"code": status, "message": f"status {status}", "status": "ERROR"}}


FRAMES = {"anthropic": anthropic_frames, "openai": openai_frames, "gemini": gemini_frames}


def make(provider: str, url: str, **kwargs: Any) -> Any:
    kwargs.setdefault("max_retries", 0)
    if provider == "anthropic":
        from highhx.agent.model.anthropic import AnthropicProvider

        return AnthropicProvider("sk-ant-test", base_url=url, **kwargs)
    if provider == "openai":
        from highhx.agent.model.openai import OpenAIProvider

        return OpenAIProvider("sk-test", base_url=f"{url}/v1", **kwargs)
    from highhx.agent.model.gemini import GeminiProvider

    return GeminiProvider("gm-test", base_url=url, **kwargs)


MODELS = {"anthropic": "claude-sonnet-5", "openai": "gpt-5", "gemini": "gemini-2.5-pro"}
PROVIDERS = list(FRAMES)


# ------------------------------------------------------------------ tests
@pytest.mark.parametrize("provider", PROVIDERS)
def test_streaming_text_and_usage(provider: str, server: WireServer) -> None:
    server.replies.append(Reply(frames=FRAMES[provider]()))
    events = list(make(provider, server.url).stream(request(model=MODELS[provider])))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    done = events[-1]
    assert text == "Hello world"
    assert isinstance(done, Completed) and done.stop_reason == "end_turn"
    assert done.usage.input_tokens == 12 and done.usage.output_tokens == 7
    assert len(server.requests) == 1


@pytest.mark.parametrize("provider", PROVIDERS)
def test_tool_calls_are_parsed(provider: str, server: WireServer) -> None:
    server.replies.append(Reply(frames=FRAMES[provider](tool=True)))
    events = list(make(provider, server.url).stream(request(tools=True, model=MODELS[provider])))
    assert any(isinstance(e, ToolCallStarted) and e.name == "read_file" for e in events)
    done = events[-1]
    assert isinstance(done, Completed) and done.stop_reason == "tool_use"
    calls = done.message.tool_calls
    assert [(c.name, c.input) for c in calls] == [("read_file", {"path": "a.py"})]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(("status", "retryable"), [(401, False), (400, False), (429, True), (500, True), (503, True)])
def test_http_errors_are_normalised(provider: str, status: int, retryable: bool, server: WireServer) -> None:
    server.replies.append(Reply(status, json_body=error_body(provider, status)))
    with pytest.raises(ModelProviderError) as info:
        list(make(provider, server.url).stream(request(model=MODELS[provider])))
    assert info.value.retryable is retryable
    if status in (401, 429):
        assert info.value.status == status


@pytest.mark.parametrize("provider", PROVIDERS)
def test_truncated_stream_is_a_retryable_error_not_a_reply(provider: str, server: WireServer) -> None:
    frames = FRAMES[provider]()
    server.replies.append(Reply(frames=frames, cut_after=2 if provider != "gemini" else 1))  # before any stop signal
    with pytest.raises(ModelProviderError) as info:
        list(make(provider, server.url).stream(request(model=MODELS[provider])))
    assert info.value.retryable


@pytest.mark.parametrize("provider", PROVIDERS)
def test_sdk_retries_only_before_streaming_starts(provider: str, server: WireServer) -> None:
    server.replies += [Reply(503, json_body=error_body(provider, 503)), Reply(frames=FRAMES[provider]())]
    events = list(make(provider, server.url, max_retries=1).stream(request(model=MODELS[provider])))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello world"  # not duplicated
    assert len(server.requests) == 2


@pytest.mark.parametrize("provider", PROVIDERS)
def test_cancellation_aborts_a_silent_stream(provider: str, server: WireServer) -> None:
    """The upstream sends one event and then goes quiet (e.g. while thinking): a cancel must
    not wait for the next event or the timeout."""
    server.replies.append(Reply(frames=FRAMES[provider](), stall_after=1 if provider == "gemini" else 3))
    token = CancellationToken()
    threading.Timer(0.5, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(OperationCancelledError):
        list(make(provider, server.url, timeout=60).stream(request(model=MODELS[provider]), cancel=token))
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("provider", PROVIDERS)
def test_silent_upstream_times_out_as_retryable(provider: str, server: WireServer) -> None:
    server.replies.append(Reply(frames=FRAMES[provider](), stall_after=1))
    started = time.monotonic()
    with pytest.raises(ModelProviderError) as info:
        list(make(provider, server.url, timeout=1.0).stream(request(model=MODELS[provider])))
    assert info.value.retryable and time.monotonic() - started < 10


@pytest.mark.parametrize("provider", PROVIDERS)
def test_unreachable_upstream_is_retryable(provider: str) -> None:
    with pytest.raises(ModelProviderError) as info:
        list(make(provider, "http://127.0.0.1:9").stream(request(model=MODELS[provider])))
    assert info.value.retryable
