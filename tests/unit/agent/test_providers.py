"""Provider adapters, the gateway wire format and SSE."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from highhx.agent.messages import Message, TextBlock, ToolCall, ToolResultBlock, Usage
from highhx.agent.model import anthropic as anthropic_adapter
from highhx.agent.model import gemini as gemini_adapter
from highhx.agent.model import openai as openai_adapter
from highhx.agent.model import registry
from highhx.agent.model.base import ModelRequest, ToolSpec, parse_tool_arguments
from highhx.agent.model.platform import PlatformProvider
from highhx.agent.model.registry import create_direct_provider, provider_info, register_provider
from highhx.agent.streaming import Completed, TextDelta, ToolCallStarted, from_wire, to_wire
from highhx.cloud import sse
from highhx.cloud.sse import ServerEvent
from highhx.core.errors import (
    CloudError,
    ConfigError,
    ModelProviderError,
    PlanRequiredError,
    QuotaExceededError,
)

TOOLS = [ToolSpec("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})]


def conversation() -> list[Message]:
    return [
        Message.user("fix it"),
        Message("assistant", [TextBlock("Reading."), ToolCall("call_1", "read_file", {"path": "a.py"})]),
        Message("user", [ToolResultBlock("call_1", "print('x')", False, "read_file")]),
    ]


# ----------------------------------------------------------------- wire formats
def test_sse_round_trip() -> None:
    frames = sse.encode("text", {"text": "hello\nworld"}) + b": keep-alive\n\n" + sse.encode("completed", {"x": 1})
    events = list(sse.decode(frames.splitlines(keepends=True)))
    assert events == [ServerEvent("text", {"text": "hello\nworld"}), ServerEvent("completed", {"x": 1})]


def test_model_events_round_trip_through_the_wire() -> None:
    message = Message(
        "assistant", [TextBlock("hi"), ToolCall("c1", "read_file", {"path": "x"})], {"anthropic": {"content": [1]}}
    )
    events = [
        TextDelta("hi"),
        ToolCallStarted("c1", "read_file"),
        Completed(message, "tool_use", Usage(3, 4, 1, 0), "m"),
    ]
    for event in events:
        assert from_wire(*to_wire(event)) == event
    assert from_wire("future-event", {}) is None


def test_request_round_trip() -> None:
    request = ModelRequest("sys", conversation(), TOOLS, "gpt-5", 1000, "high", "s1")
    assert ModelRequest.from_dict(json.loads(json.dumps(request.to_dict()))) == request


def test_parse_tool_arguments() -> None:
    assert parse_tool_arguments('{"a": 1}') == {"a": 1}
    assert parse_tool_arguments("") == {}
    assert parse_tool_arguments('{"a": ') == {"INVALID_JSON": '{"a": '}
    assert parse_tool_arguments("[1]") == {"INVALID_JSON": "[1]"}


# -------------------------------------------------------------------- anthropic
def test_anthropic_request_shape() -> None:
    params = anthropic_adapter.build_params(ModelRequest("sys", conversation(), TOOLS, effort="high"), "claude-opus-5")
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["tools"][0]["eager_input_streaming"] is True
    assert params["tools"][0]["input_schema"]["properties"]["path"]["type"] == "string"
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "high"}
    assert params["fallbacks"] == "default" and params["betas"] == [anthropic_adapter.FALLBACK_BETA]
    messages = params["messages"]
    assert messages[1]["content"][1] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "read_file",
        "input": {"path": "a.py"},
    }
    assert messages[2]["content"][0]["type"] == "tool_result" and messages[2]["content"][0]["tool_use_id"] == "call_1"
    haiku = anthropic_adapter.build_params(ModelRequest("s", [Message.user("x")]), "claude-haiku-4-5")
    assert "thinking" not in haiku and "betas" not in haiku


def test_anthropic_replays_provider_content_verbatim() -> None:
    raw = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "hi"}]
    message = Message("assistant", [TextBlock("hi")], {"anthropic": {"content": raw}})
    assert anthropic_adapter.to_anthropic_messages([message]) == [{"role": "assistant", "content": raw}]


class _Block(SimpleNamespace):
    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


class _FakeStream:
    def __init__(self, events: list[Any], final: Any) -> None:
        self.events, self.final = events, final

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> Iterator[Any]:
        return iter(self.events)

    def get_final_message(self) -> Any:
        return self.final


class _FakeMessages:
    def __init__(self, stream: _FakeStream | Exception) -> None:
        self._stream = stream
        self.params: dict[str, Any] = {}

    def stream(self, **params: Any) -> _FakeStream:
        self.params = params
        if isinstance(self._stream, Exception):
            raise self._stream
        return self._stream


def test_anthropic_stream_produces_events_and_final_message() -> None:
    final = SimpleNamespace(
        content=[
            _Block(type="thinking", thinking="", signature="s"),
            _Block(type="text", text="Let me look."),
            _Block(type="tool_use", id="toolu_1", name="read_file", input={"path": "a.py"}),
        ],
        stop_reason="tool_use",
        model="claude-opus-5",
        usage=SimpleNamespace(
            input_tokens=50, output_tokens=10, cache_read_input_tokens=40, cache_creation_input_tokens=0
        ),
    )
    events = [
        SimpleNamespace(type="text", text="Let me "),
        SimpleNamespace(type="text", text="look."),
        SimpleNamespace(
            type="content_block_start", content_block=SimpleNamespace(type="tool_use", id="toolu_1", name="read_file")
        ),
    ]
    messages = _FakeMessages(_FakeStream(events, final))
    client = SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))
    provider = anthropic_adapter.AnthropicProvider(client=client)
    out = list(provider.stream(ModelRequest("sys", [Message.user("hi")], TOOLS)))
    assert out[:3] == [TextDelta("Let me "), TextDelta("look."), ToolCallStarted("toolu_1", "read_file")]
    done = out[-1]
    assert isinstance(done, Completed) and done.stop_reason == "tool_use"
    assert done.message.tool_calls == [ToolCall("toolu_1", "read_file", {"path": "a.py"})]
    assert done.message.provider_state["anthropic"]["content"][0]["type"] == "thinking"
    assert done.usage == Usage(50, 10, 40, 0)
    assert messages.params["model"] == "claude-opus-5"


def test_anthropic_unparseable_tool_json_is_retryable() -> None:
    messages = _FakeMessages(ValueError("bad json"))
    client = SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))
    with pytest.raises(ModelProviderError) as info:
        list(anthropic_adapter.AnthropicProvider(client=client).stream(ModelRequest("s", [Message.user("x")])))
    assert info.value.retryable


# ----------------------------------------------------------------------- openai
def test_openai_message_conversion_orders_tool_results() -> None:
    messages = openai_adapter.to_openai_messages("sys", conversation())
    assert messages[0] == {"role": "system", "content": "sys"}
    assert messages[2]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path": "a.py"}'}
    assert messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": "print('x')"}


def _chunk(
    content: str | None = None, tool_calls: list[Any] | None = None, finish: str | None = None, usage: Any = None
) -> Any:
    choices = (
        []
        if usage is not None
        else [SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_calls), finish_reason=finish)]
    )
    return SimpleNamespace(choices=choices, usage=usage)


def _tc(index: int, id: str | None = None, name: str | None = None, args: str | None = None) -> Any:
    return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=args))


def test_openai_stream_assembles_tool_calls() -> None:
    chunks = [
        _chunk("Checking"),
        _chunk(tool_calls=[_tc(0, "call_a", "read_file", '{"pa')]),
        _chunk(tool_calls=[_tc(0, args='th": "a.py"}')]),
        _chunk(tool_calls=[_tc(1, "call_b", "list_files", "{")]),
        _chunk(finish="tool_calls"),
        _chunk(
            usage=SimpleNamespace(
                prompt_tokens=30, completion_tokens=7, prompt_tokens_details=SimpleNamespace(cached_tokens=10)
            )
        ),
    ]
    captured: dict[str, Any] = {}

    def create(**params: Any) -> Iterator[Any]:
        captured.update(params)
        return iter(chunks)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    out = list(
        openai_adapter.OpenAIProvider(client=client).stream(ModelRequest("s", conversation(), TOOLS, effort="low"))
    )
    done = out[-1]
    assert isinstance(done, Completed) and done.stop_reason == "tool_use"
    assert done.message.text == "Checking"
    assert done.message.tool_calls[0] == ToolCall("call_a", "read_file", {"path": "a.py"})
    assert done.message.tool_calls[1].input == {"INVALID_JSON": "{"}
    assert done.usage == Usage(30, 7, 10)
    assert captured["reasoning_effort"] == "low" and captured["stream_options"] == {"include_usage": True}
    assert captured["tools"][0]["function"]["name"] == "read_file"


# ----------------------------------------------------------------------- gemini
def test_gemini_conversion_and_stream() -> None:
    from google.genai import types

    contents = gemini_adapter.to_gemini_contents(conversation(), types)
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[1].function_call.name == "read_file"
    assert contents[2].parts[0].function_response.response == {"output": "print('x')"}

    parts = [
        types.Part(text="Looking"),
        types.Part(function_call=types.FunctionCall(name="read_file", args={"path": "a.py"})),
    ]
    chunk = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=12, candidates_token_count=3, thoughts_token_count=2, cached_content_token_count=0
        ),
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts), finish_reason=SimpleNamespace(value="STOP"))],
    )
    seen: dict[str, Any] = {}

    def generate_content_stream(**kwargs: Any) -> Iterator[Any]:
        seen.update(kwargs)
        return iter([chunk])

    client = SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate_content_stream))
    out = list(gemini_adapter.GeminiProvider(client=client).stream(ModelRequest("sys", [Message.user("x")], TOOLS)))
    done = out[-1]
    assert isinstance(done, Completed) and done.stop_reason == "tool_use"
    call = done.message.tool_calls[0]
    assert call.name == "read_file" and call.input == {"path": "a.py"} and call.id.startswith("call_")
    assert done.usage == Usage(12, 5, 0)
    assert seen["config"].automatic_function_calling.disable is True
    replay = gemini_adapter.to_gemini_contents([done.message], types)
    assert replay[0].parts[1].function_call.name == "read_file"


# --------------------------------------------------------------------- platform
class _FakePlatformClient:
    """Each call to stream() plays the next scripted connection: a list of events, optionally
    ending in an exception (a dropped connection)."""

    def __init__(self, *connections: list[Any]) -> None:
        self.connections = list(connections)
        self.calls: list[dict[str, Any]] = []
        self.posts: list[str] = []

    def stream(
        self,
        path: str,
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        cancel: Any = None,
        timeout: float = 0,
    ) -> Iterator[ServerEvent]:
        self.calls.append({"body": body, "headers": dict(headers or {})})
        for item in self.connections.pop(0):
            if isinstance(item, Exception):
                raise item
            yield item

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.posts.append(path)
        return {}


def _events(*events: Any, start: int = 1) -> list[ServerEvent]:
    return [ServerEvent(*to_wire(e), id=i) for i, e in enumerate(events, start=start)]


DONE = Completed(Message("assistant", [TextBlock("hi")]), "end_turn", Usage(1, 1))


def test_platform_provider_streams_gateway_events() -> None:
    client = _FakePlatformClient(_events(TextDelta("hi"), DONE))
    request = ModelRequest("s", [Message.user("x")], model="auto", attempt_id="attempt-0001")
    out = list(PlatformProvider(client).stream(request))  # type: ignore[arg-type]
    assert out == [TextDelta("hi"), DONE]
    call = client.calls[0]
    assert call["body"]["model"] is None and call["body"]["provider"] is None
    assert call["headers"] == {"Idempotency-Key": "attempt-0001"}


def test_platform_provider_resumes_without_duplicates() -> None:
    """A dropped connection resumes with the same key and Last-Event-ID; replayed events are dropped."""
    first = [*_events(TextDelta("a"), TextDelta("b")), CloudError("connection reset")]
    second = _events(TextDelta("a"), TextDelta("b"), TextDelta("c"), DONE)  # server replays from the start
    client = _FakePlatformClient(first, second)
    request = ModelRequest("s", [Message.user("x")], attempt_id="attempt-0002")
    out = list(PlatformProvider(client).stream(request))  # type: ignore[arg-type]
    assert out == [TextDelta("a"), TextDelta("b"), TextDelta("c"), DONE]
    assert client.calls[1]["headers"] == {"Idempotency-Key": "attempt-0002", "Last-Event-ID": "2"}


def test_platform_provider_refills_gaps() -> None:
    first = [ServerEvent(*to_wire(TextDelta("a")), id=1), ServerEvent(*to_wire(TextDelta("c")), id=3)]
    second = _events(TextDelta("b"), TextDelta("c"), DONE, start=2)
    client = _FakePlatformClient(first, second)
    out = list(PlatformProvider(client).stream(ModelRequest("s", [Message.user("x")], attempt_id="attempt-0003")))  # type: ignore[arg-type]
    assert out == [TextDelta("a"), TextDelta("b"), TextDelta("c"), DONE]
    assert client.calls[1]["headers"]["Last-Event-ID"] == "1"


def test_platform_provider_gives_up_after_bounded_resumes() -> None:
    client = _FakePlatformClient(*([CloudError("down")] for _ in range(4)))
    with pytest.raises(ModelProviderError, match="Streaming from the HighhX platform failed") as info:
        list(PlatformProvider(client).stream(ModelRequest("s", [Message.user("x")], attempt_id="attempt-0004")))  # type: ignore[arg-type]
    assert info.value.retryable and len(client.calls) == 4
    assert {c["headers"]["Idempotency-Key"] for c in client.calls} == {"attempt-0004"}


def test_platform_provider_cancellation_cancels_remotely() -> None:
    from highhx.core.errors import OperationCancelledError
    from highhx.execution.cancellation import CancellationToken

    token = CancellationToken()
    client = _FakePlatformClient([*_events(TextDelta("a")), OperationCancelledError("closed")])
    stream = PlatformProvider(client).stream(
        ModelRequest("s", [Message.user("x")], attempt_id="attempt-0005"), cancel=token
    )  # type: ignore[arg-type]
    assert next(stream) == TextDelta("a")
    token.cancel()
    with pytest.raises(OperationCancelledError):
        next(stream)
    assert client.posts == ["/v1/ai/messages/attempt-0005/cancel"]
    assert len(client.calls) == 1  # no resume after cancellation


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("plan_required", PlanRequiredError),
        ("quota_exceeded", QuotaExceededError),
        ("upstream_error", ModelProviderError),
    ],
)
def test_platform_provider_maps_gateway_errors(code: str, error: type[Exception]) -> None:
    client = _FakePlatformClient([ServerEvent("error", {"code": code, "message": "nope", "retryable": True}, id=1)])
    with pytest.raises(error):
        list(PlatformProvider(client).stream(ModelRequest("s", [Message.user("x")])))  # type: ignore[arg-type]


def test_platform_provider_detects_truncated_streams() -> None:
    client = _FakePlatformClient(*(_events(TextDelta("partial")) for _ in range(4)))
    with pytest.raises(ModelProviderError, match="closed the stream early") as info:
        list(PlatformProvider(client).stream(ModelRequest("s", [Message.user("x")])))  # type: ignore[arg-type]
    assert info.value.retryable


# --------------------------------------------------------------------- registry
def test_registry() -> None:
    assert provider_info("anthropic").default_model == "claude-opus-5"
    assert provider_info("highhx").key_env == ()
    with pytest.raises(ConfigError, match="Unknown AI provider"):
        provider_info("nope")
    assert type(create_direct_provider("openai", "k")).__name__ == "OpenAIProvider"
    sentinel = object()
    register_provider("local-llm", lambda key: sentinel)  # type: ignore[arg-type, return-value]
    try:
        assert create_direct_provider("local-llm") is sentinel
    finally:
        registry._extra_factories.pop("local-llm")
