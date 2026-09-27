"""The HighhX platform gateway: the default provider for HighhX Pro.

The platform holds provider credentials, applies the account's plan, quota and
provider settings, meters usage, and streams normalised events back. The CLI
never needs a vendor API key in this mode.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from highhx.agent.model.base import ModelRequest
from highhx.agent.streaming import Completed, ModelEvent, from_wire
from highhx.cloud import protocol
from highhx.cloud.client import PlatformClient
from highhx.core.errors import (
    AccountError,
    CloudError,
    HighhXError,
    ModelProviderError,
    OperationCancelledError,
    PlanRequiredError,
    QuotaExceededError,
)
from highhx.execution.cancellation import CancellationToken

GATEWAY_PATH = "/v1/ai/messages"
MAX_RESUMES = 3
NOT_TRANSIENT = frozenset({"provider_unavailable", "invalid_model", "invalid_provider"})


class PlatformProvider:
    name = "highhx"
    default_model = "auto"

    def __init__(self, client: PlatformClient, *, upstream: str | None = None) -> None:
        self.client = client
        self.upstream = upstream
        """Preferred upstream provider (anthropic / openai / gemini); ``None`` uses the account setting."""

    def stream(self, request: ModelRequest, *, cancel: CancellationToken | None = None) -> Iterator[ModelEvent]:
        """Stream one model attempt. If the connection drops, reconnect with the same idempotency
        key and ``Last-Event-ID``: the platform replays the events this attempt already produced
        (it never calls the model twice for one key, and meters it once). Events are ordered by
        id; replayed duplicates are dropped. Nothing is executed here — tools run only after the
        final ``completed`` event, so resuming can never run a tool twice."""
        body = request.to_dict()
        if body.get("model") == "auto":
            body["model"] = None
        body["provider"] = self.upstream
        key = request.attempt_id or uuid.uuid4().hex
        last_id = 0
        resumes = 0
        while True:
            headers = {protocol.IDEMPOTENCY_HEADER: key}
            if last_id:
                headers["Last-Event-ID"] = str(last_id)
            try:
                for server_event in self.client.stream(GATEWAY_PATH, body, headers=headers, cancel=cancel):
                    if cancel is not None and cancel.cancelled:
                        raise OperationCancelledError("Model response cancelled.")
                    if server_event.id is not None:
                        if server_event.id <= last_id:
                            continue  # already delivered before a reconnect
                        if server_event.id != last_id + 1:
                            raise _Gap(last_id, server_event.id)
                        last_id = server_event.id
                    if server_event.event == "error":
                        raise _gateway_error(server_event.data)
                    event = from_wire(server_event.event, server_event.data)
                    if event is None:
                        continue
                    yield event
                    if isinstance(event, Completed):
                        return
            except OperationCancelledError:
                self._cancel_remote(key)
                raise
            except (AccountError, PlanRequiredError):
                raise
            except CloudError as exc:
                if exc.status is not None:
                    # The platform answered with an error: not a dropped stream, so no resume.
                    # 5xx / 429 are transient (the session retries with backoff); 4xx are not.
                    retryable = (exc.status >= 500 or exc.status == 429) and exc.code not in NOT_TRANSIENT
                    raise ModelProviderError(
                        exc.message, hint=exc.hint, retryable=retryable, status=exc.status
                    ) from None
                if resumes >= MAX_RESUMES or (cancel is not None and cancel.cancelled):
                    raise ModelProviderError(
                        f"Streaming from the HighhX platform failed: {exc.message}", retryable=True, connection=True
                    ) from None
            except _Gap as exc:
                reason = str(exc)
                if resumes >= MAX_RESUMES or (cancel is not None and cancel.cancelled):
                    raise ModelProviderError(
                        f"Streaming from the HighhX platform failed: {reason}", retryable=True
                    ) from None
            else:
                if resumes >= MAX_RESUMES:
                    raise ModelProviderError("The HighhX platform closed the stream early.", retryable=True)
            resumes += 1
            if cancel is not None and cancel.wait(min(0.5 * resumes, 2.0)):
                self._cancel_remote(key)
                raise OperationCancelledError("Model response cancelled.")

    def _cancel_remote(self, key: str) -> None:
        """Best effort: tell the platform to stop the upstream call for this attempt."""
        try:
            self.client.post(f"{GATEWAY_PATH}/{key}/cancel")
        except HighhXError:
            pass


class _Gap(Exception):
    def __init__(self, last: int, got: int) -> None:
        super().__init__(f"missed events {last + 1}..{got - 1}")


def _gateway_error(data: dict[str, object]) -> Exception:
    code = str(data.get("code") or "")
    message = str(data.get("message") or "The HighhX AI gateway returned an error.")
    hint = str(data["hint"]) if data.get("hint") else None
    if code == "plan_required":
        return PlanRequiredError(message, hint=hint or "Upgrade with `highhx account upgrade`.")
    if code == "unauthorized":
        return AccountError(message, hint=hint or "Run `highhx login`.")
    if code == "quota_exceeded":
        return QuotaExceededError(message, hint=hint or "See `highhx account usage`.")
    return ModelProviderError(message, hint=hint, retryable=bool(data.get("retryable")))
