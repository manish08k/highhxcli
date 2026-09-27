"""OpenAI provider (Chat Completions with streamed tool calls), using the official ``openai`` SDK."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from highhx.agent.messages import Message, StopReason, TextBlock, ToolCall, ToolResultBlock, Usage
from highhx.agent.model.base import (
    DEFAULT_SDK_RETRIES,
    DEFAULT_TIMEOUT,
    Aborter,
    ModelRequest,
    parse_tool_arguments,
    pump,
    transport_error,
    truncated,
)
from highhx.agent.streaming import Completed, ModelEvent, TextDelta, ToolCallStarted
from highhx.core.errors import ModelProviderError
from highhx.execution.cancellation import CancellationToken

DEFAULT_MODEL = "gpt-5"
MODELS = ("gpt-5", "gpt-5-mini", "o4-mini")

_STOP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _import_sdk() -> Any:
    try:
        import openai
    except ImportError:
        raise ModelProviderError(
            "The OpenAI provider needs the `openai` package.",
            hint='Install it with `pip install "highhxcli[openai]"`, or use the HighhX provider.',
        ) from None
    return openai


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        if message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.text or None}
            calls = message.tool_calls
            if calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.input)},
                    }
                    for call in calls
                ]
            out.append(entry)
            continue
        # Tool results become `tool` messages; they must directly follow the assistant message.
        for block in message.blocks:
            if isinstance(block, ToolResultBlock):
                content = f"ERROR: {block.content}" if block.is_error else block.content
                out.append({"role": "tool", "tool_call_id": block.tool_call_id, "content": content})
        text = message.text
        if text:
            out.append({"role": "user", "content": text})
    return out


def build_params(request: ModelRequest, model: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": model,
        "messages": to_openai_messages(request.system, request.messages),
        "max_completion_tokens": request.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.tools:
        params["tools"] = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in request.tools
        ]
    if request.effort in ("low", "medium", "high"):
        params["reasoning_effort"] = request.effort
    return params


class OpenAIProvider:
    name = "openai"
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
            openai = _import_sdk()
            kwargs: dict[str, Any] = {"max_retries": self._max_retries, "timeout": self._timeout}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self._base_url:
                kwargs["base_url"] = self._base_url
            try:
                self._client = openai.OpenAI(**kwargs)
            except openai.OpenAIError as exc:
                raise ModelProviderError(
                    f"OpenAI credentials are not configured ({exc}).",
                    hint="Set OPENAI_API_KEY, or use the HighhX provider (`/model highhx`).",
                ) from None
        return self._client

    def stream(self, request: ModelRequest, *, cancel: CancellationToken | None = None) -> Iterator[ModelEvent]:
        openai = _import_sdk()
        client = self._sdk_client()
        model = request.model or self.default_model
        text: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish: str | None = None
        usage = Usage()
        params = build_params(request, model)

        def produce(aborter: Aborter) -> Iterator[Any]:
            stream = client.chat.completions.create(**params)
            if getattr(stream, "response", None) is not None:
                aborter.response(stream.response)
            try:
                yield from stream
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()

        try:
            for chunk in pump(produce, cancel):
                if chunk.usage is not None:
                    details = getattr(chunk.usage, "prompt_tokens_details", None)
                    usage = Usage(
                        int(chunk.usage.prompt_tokens or 0),
                        int(chunk.usage.completion_tokens or 0),
                        int(getattr(details, "cached_tokens", 0) or 0) if details else 0,
                    )
                for choice in chunk.choices or []:
                    delta = choice.delta
                    if delta is not None and delta.content:
                        text.append(delta.content)
                        yield TextDelta(delta.content)
                    for tc in (delta.tool_calls if delta is not None else None) or []:
                        slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function is not None:
                            if tc.function.name:
                                slot["name"] = tc.function.name
                                yield ToolCallStarted(slot["id"], slot["name"])
                            if tc.function.arguments:
                                slot["arguments"] += tc.function.arguments
                    if choice.finish_reason:
                        finish = choice.finish_reason
            if finish is None:  # no finish_reason: the stream was cut off
                raise truncated("OpenAI")
        except openai.AuthenticationError:
            raise ModelProviderError("OpenAI rejected the API key.", hint="Check OPENAI_API_KEY.", status=401) from None
        except openai.NotFoundError:
            raise ModelProviderError(
                f"Unknown OpenAI model '{model}'.", hint=f"Try one of: {', '.join(MODELS)}.", status=404
            ) from None
        except openai.RateLimitError:
            raise ModelProviderError("OpenAI rate limit reached.", retryable=True, status=429) from None
        except openai.BadRequestError as exc:
            raise ModelProviderError(f"OpenAI rejected the request: {exc.message}", status=400) from None
        except openai.APIStatusError as exc:
            raise ModelProviderError(
                f"OpenAI API error ({exc.status_code}): {exc.message}",
                retryable=exc.status_code >= 500,
                status=exc.status_code,
            ) from None
        except openai.APITimeoutError:
            raise ModelProviderError("The OpenAI API timed out.", retryable=True) from None
        except openai.APIConnectionError:
            raise ModelProviderError("Cannot reach the OpenAI API.", retryable=True) from None
        except openai.APIError as exc:  # e.g. an error object in the middle of the stream
            raise ModelProviderError(f"OpenAI API error: {exc}", retryable=True) from None
        except Exception as exc:
            mapped = transport_error(exc, "OpenAI")
            if mapped is None:
                raise
            raise mapped from None
        blocks: list[TextBlock | ToolCall | ToolResultBlock] = []
        if text:
            blocks.append(TextBlock("".join(text)))
        for index in sorted(calls):
            slot = calls[index]
            blocks.append(
                ToolCall(slot["id"] or f"call_{index}", slot["name"], parse_tool_arguments(slot["arguments"]))
            )
        stop = _STOP.get(finish or "stop", "end_turn")
        if stop == "end_turn" and calls:
            stop = "tool_use"
        yield Completed(Message("assistant", blocks), stop, usage, model)
