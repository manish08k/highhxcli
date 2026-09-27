"""EXTERNAL REAL INTEGRATION: one minimal request to each real vendor API.

Opt-in (costs a few tokens): HIGHHX_TEST_LIVE_PROVIDERS=1 plus the vendor's key —
ANTHROPIC_API_KEY, OPENAI_API_KEY and/or GEMINI_API_KEY. Skipped otherwise.
"""

from __future__ import annotations

import os

import pytest

from highhx.agent.messages import Message
from highhx.agent.model.base import ModelRequest
from highhx.agent.model.registry import create_direct_provider
from highhx.agent.streaming import Completed, TextDelta
from highhx.core.errors import ModelProviderError, OperationCancelledError
from highhx.execution.cancellation import CancellationToken

KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}


def _provider(name: str, key: str | None = None) -> object:
    if not os.environ.get("HIGHHX_TEST_LIVE_PROVIDERS"):
        pytest.skip("set HIGHHX_TEST_LIVE_PROVIDERS=1 (and a vendor key) to call real providers")
    key = key or os.environ.get(KEYS[name])
    if not key:
        pytest.skip(f"{KEYS[name]} is not set")
    return create_direct_provider(name, key)


@pytest.mark.parametrize("name", list(KEYS))
def test_real_streaming_reply_with_usage(name: str) -> None:
    provider = _provider(name)
    request = ModelRequest("Answer with one word.", [Message.user("Say OK.")], max_tokens=512)
    events = list(provider.stream(request))  # type: ignore[attr-defined]
    done = events[-1]
    assert isinstance(done, Completed)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)).strip()
    assert done.usage.input_tokens > 0 and done.usage.output_tokens > 0


@pytest.mark.parametrize("name", list(KEYS))
def test_real_bad_key_is_a_non_retryable_auth_error(name: str) -> None:
    provider = _provider(name, key="invalid-key-for-highhx-test")  # highhx:allow-secret (not a credential)
    with pytest.raises(ModelProviderError) as info:
        list(provider.stream(ModelRequest("", [Message.user("hi")], max_tokens=64)))  # type: ignore[attr-defined]
    assert not info.value.retryable


@pytest.mark.parametrize("name", list(KEYS))
def test_real_stream_can_be_cancelled(name: str) -> None:
    provider = _provider(name)
    token = CancellationToken()
    request = ModelRequest("", [Message.user("Count slowly from 1 to 300, one number per line.")], max_tokens=4000)
    with pytest.raises(OperationCancelledError):
        for event in provider.stream(request, cancel=token):  # type: ignore[attr-defined]
            if isinstance(event, TextDelta):
                token.cancel()
