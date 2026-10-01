"""Anthropic (Claude) provider, using the official ``anthropic`` SDK."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from highhx.agent.messages import ImageBlock, Message, StopReason, TextBlock, ToolCall, ToolResultBlock, Usage
from highhx.agent.model.base import (
    DEFAULT_SDK_RETRIES,
    DEFAULT_TIMEOUT,
    Aborter,
    ModelRequest,
    pump,
    transport_error,
    truncated,
)
from highhx.agent.streaming import Completed, ModelEvent, TextDelta, ToolCallStarted
from highhx.core.errors import ModelProviderError
from highhx.execution.cancellation import CancellationToken

DEFAULT_MODEL = "claude-opus-5"
MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-fable-5-1", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that support server-side refusal fallbacks (`fallbacks: "default"`).
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")

_STOP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "pause_turn": "pause",
}


def _import_sdk() -> Any:
    try:
        import anthropic
    except ImportError:
        raise ModelProviderError(
            "The Anthropic provider needs the `anthropic` package.",
            hint='Install it with `pip install "highhxcli[anthropic]"`, or use the HighhX provider.',
        ) from None
    return anthropic


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        replay = message.provider_state.get("anthropic")
        if message.role == "assistant" and isinstance(replay, dict) and isinstance(replay.get("content"), list):
            # Replay the exact content (thinking blocks are only valid when echoed unchanged).
            out.append({"role": "assistant", "content": replay["content"]})
            continue
        content: list[dict[str, Any]] = []
        for block in message.blocks:
            if isinstance(block, TextBlock):
                if block.text:
                    content.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCall):
                content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
            elif isinstance(block, ToolResultBlock):
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.tool_call_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    }
                )
            elif isinstance(block, ImageBlock):
                if block.label:
                    content.append({"type": "text", "text": block.label})
                content.append(
                    {"type": "image", "source": {"type": "base64", "media_type": block.media_type, "data": block.data}}
                )
        if content:
            out.append({"role": message.role, "content": content})
    return out


def build_params(request: ModelRequest, model: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": request.max_tokens,
        # A frozen system prompt + deterministic tool list keep the prompt cache warm.
        "system": [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
        "messages": to_anthropic_messages(request.messages),
    }
    if request.tools:
        params["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
                # Stream large inputs (file bodies) as generated; the session validates every input.
                "eager_input_streaming": True,
            }
            for tool in request.tools
        ]
    if not model.startswith("claude-haiku"):
        params["thinking"] = {"type": "adaptive"}
    if request.effort:
        params["output_config"] = {"effort": request.effort}
    if model in _FALLBACK_MODELS:
        # Server-side refusal fallback: a declined request is re-run on a fallback model.
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


class AnthropicProvider:
    name = "anthropic"
    default_model = DEFAULT_MODEL

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        client: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_SDK_RETRIES,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._client = client
        self._timeout = timeout
        self._max_retries = max_retries

    def _sdk_client(self) -> Any:
        if self._client is None:
            anthropic = _import_sdk()
            kwargs: dict[str, Any] = {"max_retries": self._max_retries, "timeout": self._timeout}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self._base_url:
                kwargs["base_url"] = self._base_url
            try:
                self._client = anthropic.Anthropic(**kwargs)
            except anthropic.AnthropicError as exc:
                raise ModelProviderError(
                    f"Anthropic credentials are not configured ({exc}).",
                    hint="Set ANTHROPIC_API_KEY, or use the HighhX provider (`/model highhx`).",
                ) from None
        return self._client

    def stream(self, request: ModelRequest, *, cancel: CancellationToken | None = None) -> Iterator[ModelEvent]:
        anthropic = _import_sdk()
        client = self._sdk_client()
        model = request.model or self.default_model
        params = build_params(request, model)
        api = client.beta.messages if "betas" in params else client.messages

        def produce(aborter: Aborter) -> Iterator[Any]:
            with api.stream(**params) as stream:
                if getattr(stream, "response", None) is not None:
                    aborter.response(stream.response)
                for event in stream:
                    if event.type == "text":
                        yield TextDelta(event.text)
                    elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                        yield ToolCallStarted(event.content_block.id, event.content_block.name)
                yield stream.get_final_message()

        final: Any = None
        try:
            for item in pump(produce, cancel):
                if isinstance(item, (TextDelta, ToolCallStarted)):
                    yield item
                else:
                    final = item
            if final is None or final.stop_reason is None:  # no message_delta/message_stop: cut off
                raise truncated("Anthropic")
        except ValueError as exc:
            # Tool input JSON the SDK could not parse at all: re-issue the turn.
            raise ModelProviderError(f"Claude produced an unreadable tool call ({exc}).", retryable=True) from None
        except anthropic.AuthenticationError:
            raise ModelProviderError(
                "Anthropic rejected the API key.", hint="Check ANTHROPIC_API_KEY.", status=401
            ) from None
        except anthropic.PermissionDeniedError as exc:
            raise ModelProviderError(f"Anthropic denied the request: {exc.message}", status=403) from None
        except anthropic.NotFoundError:
            raise ModelProviderError(
                f"Unknown Anthropic model '{model}'.", hint=f"Try one of: {', '.join(MODELS)}.", status=404
            ) from None
        except anthropic.RateLimitError:
            raise ModelProviderError("Anthropic rate limit reached.", retryable=True, status=429) from None
        except anthropic.BadRequestError as exc:
            raise ModelProviderError(f"Anthropic rejected the request: {exc.message}", status=400) from None
        except anthropic.APIStatusError as exc:
            raise ModelProviderError(
                f"Anthropic API error ({exc.status_code}): {exc.message}",
                retryable=exc.status_code >= 500,
                status=exc.status_code,
            ) from None
        except anthropic.APITimeoutError:
            raise ModelProviderError("The Anthropic API timed out.", retryable=True) from None
        except anthropic.APIConnectionError:
            raise ModelProviderError("Cannot reach the Anthropic API.", retryable=True) from None
        except anthropic.APIError as exc:  # e.g. an `error` event in the middle of the stream
            raise ModelProviderError(f"Anthropic API error: {exc}", retryable=True) from None
        except Exception as exc:
            mapped = transport_error(exc, "Anthropic")
            if mapped is None:
                raise
            raise mapped from None
        yield completed_from_final(final, model)


def completed_from_final(final: Any, model: str) -> Completed:
    blocks: list[TextBlock | ToolCall | ToolResultBlock] = []
    raw: list[dict[str, Any]] = []
    for block in final.content:
        raw.append(block.to_dict())
        if block.type == "text":
            blocks.append(TextBlock(block.text))
        elif block.type == "tool_use":
            args = block.input if isinstance(block.input, dict) else {"INVALID_JSON": str(block.input)}
            blocks.append(ToolCall(block.id, block.name, args))
    usage = final.usage
    return Completed(
        Message("assistant", blocks, {"anthropic": {"content": raw}}),
        _STOP.get(str(final.stop_reason), "end_turn"),
        Usage(
            int(getattr(usage, "input_tokens", 0) or 0),
            int(getattr(usage, "output_tokens", 0) or 0),
            int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        ),
        str(getattr(final, "model", "") or model),
    )
