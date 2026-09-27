"""Google Gemini provider, using the official ``google-genai`` SDK."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

from highhx.agent.messages import Message, StopReason, TextBlock, ToolCall, ToolResultBlock, Usage
from highhx.agent.model.base import DEFAULT_SDK_RETRIES, DEFAULT_TIMEOUT, ModelRequest, cancellable, truncated
from highhx.agent.streaming import Completed, ModelEvent, TextDelta, ToolCallStarted
from highhx.core.errors import ModelProviderError, OperationCancelledError
from highhx.execution.cancellation import CancellationToken

DEFAULT_MODEL = "gemini-2.5-pro"
MODELS = ("gemini-2.5-pro", "gemini-2.5-flash")

_STOP: dict[str, StopReason] = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "refusal",
    "PROHIBITED_CONTENT": "refusal",
    "BLOCKLIST": "refusal",
    "RECITATION": "refusal",
}


def _import_sdk() -> tuple[Any, Any, Any]:
    try:
        from google import genai
        from google.genai import errors, types
    except ImportError:
        raise ModelProviderError(
            "The Gemini provider needs the `google-genai` package.",
            hint='Install it with `pip install "highhxcli[gemini]"`, or use the HighhX provider.',
        ) from None
    return genai, types, errors


def to_gemini_contents(messages: list[Message], types: Any) -> list[Any]:
    contents: list[Any] = []
    for message in messages:
        replay = message.provider_state.get("gemini")
        if message.role == "assistant" and isinstance(replay, dict) and isinstance(replay.get("parts"), list):
            # Replay parts verbatim so thought signatures survive multi-turn tool use.
            parts = [types.Part.model_validate(p) for p in replay["parts"]]
            contents.append(types.Content(role="model", parts=parts))
            continue
        parts = []
        for block in message.blocks:
            if isinstance(block, TextBlock) and block.text:
                parts.append(types.Part(text=block.text))
            elif isinstance(block, ToolCall):
                parts.append(
                    types.Part(function_call=types.FunctionCall(id=block.id, name=block.name, args=block.input))
                )
            elif isinstance(block, ToolResultBlock):
                key = "error" if block.is_error else "output"
                parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=block.tool_call_id, name=block.name or "tool", response={key: block.content}
                        )
                    )
                )
        if parts:
            contents.append(types.Content(role="model" if message.role == "assistant" else "user", parts=parts))
    return contents


class GeminiProvider:
    name = "gemini"
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

    def _sdk_client(self) -> tuple[Any, Callable[[], None] | None]:
        """The SDK client and a function that aborts its in-flight request.

        Each call gets its own HTTP connection pool so that cancelling one response (by closing
        its pool from another thread) cannot affect any other request."""
        if self._client is not None:
            return self._client, None
        genai, types, _errors = _import_sdk()
        import httpx

        http = httpx.Client(timeout=httpx.Timeout(self._timeout, connect=min(self._timeout, 30.0)))
        options = types.HttpOptions(
            base_url=self._base_url,
            timeout=int(self._timeout * 1000),
            httpx_client=http,
            retry_options=types.HttpRetryOptions(attempts=self._max_retries + 1),
        )
        try:
            client = (
                genai.Client(api_key=self._api_key, http_options=options)
                if self._api_key
                else genai.Client(http_options=options)
            )
        except ValueError as exc:
            http.close()
            raise ModelProviderError(
                f"Gemini credentials are not configured ({exc}).",
                hint="Set GEMINI_API_KEY, or use the HighhX provider (`/model highhx`).",
            ) from None
        return client, http.close

    def stream(self, request: ModelRequest, *, cancel: CancellationToken | None = None) -> Iterator[ModelEvent]:
        _genai, types, errors = _import_sdk()
        import httpx

        client, close = self._sdk_client()
        model = request.model or self.default_model
        config = types.GenerateContentConfig(
            system_instruction=request.system,
            max_output_tokens=request.max_tokens,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name, description=t.description, parameters_json_schema=t.parameters
                        )
                        for t in request.tools
                    ]
                )
            ]
            if request.tools
            else None,
            # HighhX executes tools itself (with approvals); never let the SDK call functions.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        parts: list[Any] = []
        text: list[str] = []
        finish: str | None = None
        usage = Usage()
        try:
            try:
                with cancellable(cancel, close):
                    for chunk in client.models.generate_content_stream(
                        model=model, contents=to_gemini_contents(request.messages, types), config=config
                    ):
                        if cancel is not None and cancel.cancelled:
                            raise OperationCancelledError("Model response cancelled.")
                        meta = chunk.usage_metadata
                        if meta is not None:
                            usage = Usage(
                                int(meta.prompt_token_count or 0),
                                int(
                                    (meta.candidates_token_count or 0) + (getattr(meta, "thoughts_token_count", 0) or 0)
                                ),
                                int(getattr(meta, "cached_content_token_count", 0) or 0),
                            )
                        for candidate in chunk.candidates or []:
                            if candidate.finish_reason is not None:
                                finish = str(getattr(candidate.finish_reason, "value", candidate.finish_reason))
                            for part in (candidate.content.parts if candidate.content else None) or []:
                                parts.append(part)
                                if part.text and not part.thought:
                                    text.append(part.text)
                                    yield TextDelta(part.text)
                                if part.function_call is not None:
                                    if not part.function_call.id:
                                        part.function_call.id = f"call_{uuid.uuid4().hex[:12]}"
                                    yield ToolCallStarted(part.function_call.id, part.function_call.name or "")
            finally:
                if close is not None:
                    close()
            if finish is None:  # no finishReason: the stream was cut off
                raise truncated("Gemini")
        except errors.ClientError as exc:
            code = int(getattr(exc, "code", 400) or 400)
            if code in (401, 403):
                raise ModelProviderError(
                    "Gemini rejected the API key.", hint="Check GEMINI_API_KEY.", status=code
                ) from None
            if code == 429:
                raise ModelProviderError("Gemini rate limit reached.", retryable=True, status=429) from None
            raise ModelProviderError(f"Gemini rejected the request: {exc}", status=code) from None
        except errors.ServerError as exc:
            code = int(getattr(exc, "code", 500) or 500)
            raise ModelProviderError(f"Gemini API error ({code}): {exc}", retryable=True, status=code) from None
        except errors.APIError as exc:
            raise ModelProviderError(f"Gemini API error: {exc}", retryable=True) from None
        except httpx.TimeoutException:
            raise ModelProviderError("The Gemini API timed out.", retryable=True) from None
        except httpx.TransportError:
            raise ModelProviderError("Cannot reach the Gemini API.", retryable=True) from None
        blocks: list[TextBlock | ToolCall | ToolResultBlock] = []
        if text:
            blocks.append(TextBlock("".join(text)))
        calls = [p.function_call for p in parts if p.function_call is not None]
        blocks.extend(ToolCall(str(c.id), str(c.name or ""), dict(c.args or {})) for c in calls)
        stop = "tool_use" if calls else _STOP.get(finish or "STOP", "end_turn")
        state = {"gemini": {"parts": [p.model_dump(mode="json", exclude_none=True) for p in parts]}}
        yield Completed(Message("assistant", blocks, state), stop, usage, model)
