"""The streaming AI gateway used by `highhx agent`.

POST /v1/ai/messages                  start (or resume) one model attempt
POST /v1/ai/messages/{key}/cancel     cancel it

Every request is authenticated, requires the Pro `agent` feature (and
`agent.computer_use` when computer-use tools are offered to the model), is
subject to the monthly allowance and a concurrency limit, and is metered once per
idempotency key. A reconnect with the same key and `Last-Event-ID` replays the
missed events of the running (or recently finished) attempt without calling the
model again.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from highhx.agent.model.base import ModelRequest
from highhx.agent.streaming import Completed
from highhx.cloud import sse
from highhx.cloud.plans import AGENT, AGENT_COMPUTER_USE
from highhx.cloud.protocol import IDEMPOTENCY_HEADER
from highhx_platform import accounts
from highhx_platform.config import Settings
from highhx_platform.deps import Principal, current_principal, get_db, get_settings, problem, require_feature
from highhx_platform.gateway import GatewayError, fallback_routes, resolve_route, run_upstream
from highhx_platform.models import UsageRecord, utcnow
from highhx_platform.streams import StreamStore

router = APIRouter(prefix="/v1/ai", tags=["ai"])

COMPUTER_TOOLS = frozenset({"computer_observe", "computer_act", "browser_open", "app_open"})
KEEPALIVE_SECONDS = 15.0
_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
PENDING_WINDOW = timedelta(minutes=15)


class GatewayBody(BaseModel):
    system: str = Field(default="", max_length=400_000)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=5_000)
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    model: str | None = Field(default=None, max_length=100)
    provider: str | None = Field(default=None, max_length=32)
    max_tokens: int = Field(default=32_000, ge=256, le=128_000)
    effort: str | None = Field(default=None, pattern=r"^(low|medium|high|xhigh|max)$")
    session_id: str | None = Field(default=None, max_length=64)
    attempt_id: str | None = Field(default=None, max_length=64)


def _store(request: Request) -> StreamStore:
    store: StreamStore = request.app.state.streams
    return store


def _reader(store: StreamStore, request_id: str, after: int) -> AsyncIterator[bytes]:
    async def generate() -> AsyncIterator[bytes]:
        index = after
        last_touch = 0.0
        last_sent = time.monotonic()
        while True:
            now = time.monotonic()
            touch = now - last_touch >= store.interval
            snap = await asyncio.to_thread(store.read, request_id, index, touch=touch)
            if touch:
                last_touch = now
            if snap.frames:
                index += len(snap.frames)
                last_sent = time.monotonic()
                for frame in snap.frames:
                    yield frame
                continue  # there may be more than one page
            if snap.done:
                return
            if snap.owner_stale:
                await asyncio.to_thread(store.fail_orphan, request_id)
                continue
            if time.monotonic() - last_sent >= KEEPALIVE_SECONDS:
                last_sent = time.monotonic()
                yield sse.KEEPALIVE
            await asyncio.sleep(store.poll)

    # A client that disconnects simply stops reading: the owner keeps the upstream call
    # running for the grace period so the client can resume (on any instance), then cancels it.
    return generate()


def _stream(store: StreamStore, request_id: str, after: int, provider: str) -> StreamingResponse:
    return StreamingResponse(
        _reader(store, request_id, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "X-HighhX-Provider": provider},
    )


def _existing(store: StreamStore, request_id: str, after: int) -> StreamingResponse:
    """Resume or duplicate delivery of a known stream: replay only, never call the model again."""
    row = store.get(request_id)
    if row is None or row.events_pruned:
        if after:
            raise problem(410, "stream_expired", "That stream can no longer be resumed.", "Retry the request.")
        raise problem(409, "duplicate_request", "This request was already processed.", "Send a new request.")
    return _stream(store, request_id, after, "resumed")


@router.post("/messages")
def messages(
    body: GatewayBody,
    request: Request,
    principal: Principal = require_feature(AGENT, "The HighhX AI agent"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    user = principal.user
    store = _store(request)
    store.prune()
    key = request.headers.get(IDEMPOTENCY_HEADER) or body.attempt_id or uuid.uuid4().hex
    if not _KEY.match(key):
        raise problem(400, "invalid_idempotency_key", "Idempotency-Key must be 8-64 characters [A-Za-z0-9_-].")
    request_id = f"{user.id}:{key}"
    last_event = request.headers.get("last-event-id") or "0"
    after = int(last_event) if last_event.isdigit() else 0

    if store.get(request_id) is not None:
        return _existing(store, request_id, after)
    if after:
        raise problem(410, "stream_expired", "That stream can no longer be resumed.", "Retry the request.")
    if db.scalar(select(UsageRecord.id).where(UsageRecord.request_id == request_id)) is not None:
        # Claimed meanwhile by a concurrent duplicate (attach to it), or long ago (409).
        return _existing(store, request_id, after)

    tool_names = {str(t.get("name")) for t in body.tools}
    if tool_names & COMPUTER_TOOLS and AGENT_COMPUTER_USE not in principal.plan.features:
        raise problem(
            402, "plan_required", "Computer use requires HighhX Pro.", "Upgrade with `highhx account upgrade`."
        )
    length = int(request.headers.get("content-length") or 0)
    if length > settings.max_request_bytes:
        raise problem(
            413, "too_large", "The conversation is too large for one request.", "Start a new session with /clear."
        )
    plan = principal.plan
    summary = accounts.usage_summary(db, user)
    if plan.monthly_tokens and summary.tokens_used >= plan.monthly_tokens:
        if store.get(request_id) is not None:  # a concurrent duplicate claimed the key meanwhile
            return _existing(store, request_id, after)
        raise problem(
            429,
            "quota_exceeded",
            f"You have used this period's {plan.monthly_tokens:,} AI tokens.",
            f"Your allowance renews on {summary.period_end.date().isoformat()}. See `highhx account usage`.",
        )
    pending = db.scalar(
        select(func.count(UsageRecord.id)).where(
            UsageRecord.user_id == user.id,
            UsageRecord.status == "pending",
            UsageRecord.created_at >= utcnow() - PENDING_WINDOW,
        )
    )
    if int(pending or 0) >= settings.max_concurrent_streams:
        if store.get(request_id) is not None:  # a concurrent duplicate claimed the key meanwhile
            return _existing(store, request_id, after)
        raise problem(
            429,
            "too_many_concurrent",
            f"At most {settings.max_concurrent_streams} AI requests may run at once.",
            "Wait for a running request to finish.",
        )
    agent_settings = (user.settings or {}).get("agent") or {}
    try:
        route = resolve_route(
            settings,
            requested_provider=body.provider,
            requested_model=body.model,
            account_upstream=agent_settings.get("upstream"),
        )
        model_request = ModelRequest.from_dict(body.model_dump())
    except GatewayError as exc:
        raise problem(exc.status, exc.code, exc.message, exc.hint) from None
    except (KeyError, ValueError) as exc:
        raise problem(400, "invalid_request", f"Malformed conversation: {exc}") from None

    # Claim the key and create the shared stream in one transaction: a concurrent duplicate
    # (on this or another platform instance) fails on the unique constraint and attaches as a reader.
    db.add(store.new_row(request_id, user.id, route.provider))
    db.add(
        UsageRecord(
            user_id=user.id,
            request_id=request_id,
            session_id=body.session_id,
            provider=route.provider,
            model=route.model,
            status="pending",
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _existing(store, request_id, after)

    database = request.app.state.db

    def record(provider: str, model: str, completed: Completed | None, status: str) -> None:
        usage = completed.usage if completed else None
        with database.sessions() as session:
            session.execute(
                update(UsageRecord)
                .where(UsageRecord.request_id == request_id)
                .values(
                    provider=provider,
                    model=(completed.model or model) if completed else model,
                    input_tokens=usage.input_tokens if usage else 0,
                    output_tokens=usage.output_tokens if usage else 0,
                    cache_read_tokens=usage.cache_read_tokens if usage else 0,
                    status=status,
                    finished_at=utcnow(),
                )
            )
            session.commit()

    routes = fallback_routes(settings, route, pinned_model=bool(body.model))
    factory = request.app.state.provider_factory
    store.start(request_id, lambda run: run_upstream(run, model_request, routes, settings, factory, record))
    return _stream(store, request_id, after, route.provider)


@router.post("/messages/{key}/cancel")
def cancel(key: str, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Stop the upstream call for one of *your* attempts. Idempotent."""
    status = _store(request).request_cancel(f"{principal.user.id}:{key}", principal.user.id)
    if status is None:
        return {"cancelled": False, "status": "unknown"}
    return {"cancelled": True, "status": status}
