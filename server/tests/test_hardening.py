"""Protocol, streaming/resume, idempotency, cancellation, entitlements, auth lifecycle, billing,
usage, isolation, session lifecycle and migrations — each on SQLite and PostgreSQL."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from helpers import GatedUpstream, LiveServer, Upstream, auth, text_reply
from highhx.agent.messages import Message, TextBlock, Usage
from highhx.agent.streaming import Completed, TextDelta
from highhx.cloud import sse
from highhx.cloud.protocol import PROTOCOL_HEADER, PROTOCOL_VERSION
from highhx_platform.app import create_app
from highhx_platform.config import Settings
from highhx_platform.models import ApiToken, AuditEvent, Subscription, UsageRecord, User, utcnow

DONE = Completed(Message("assistant", [TextBlock("done")]), "end_turn", Usage(300, 30), "claude-opus-5")


def body(**extra: Any) -> dict[str, Any]:
    return {"system": "s", "messages": [Message.user("hi").to_dict()], "tools": [], **extra}


def read_events(response: Any, limit: int | None = None) -> list[sse.ServerEvent]:
    events: list[sse.ServerEvent] = []
    for event in sse.decode(response.iter_lines()):
        events.append(event)
        if limit is not None and len(events) >= limit:
            break
    return events


def gated_client(settings: Settings, gated: GatedUpstream) -> TestClient:
    app = create_app(settings, provider_factory=gated.factory)
    return TestClient(app, base_url="https://platform.test", headers={PROTOCOL_HEADER: PROTOCOL_VERSION})


def db_session(client: TestClient) -> Any:
    return client.app.state.db.sessions()


def usage_rows(client: TestClient) -> list[UsageRecord]:
    with db_session(client) as s:
        return list(s.scalars(select(UsageRecord).order_by(UsageRecord.created_at)).all())


# ------------------------------------------------------------------ protocol
@pytest.mark.parametrize(
    ("header", "status", "code"),
    [
        (PROTOCOL_VERSION, 200, None),
        ("1.0", 200, None),  # older minor of the same major is supported
        ("2.0", 426, "client_unsupported"),
        ("0.9", 426, "client_outdated"),
        ("banana", 426, "client_unsupported"),
        (None, 426, "client_unsupported"),
    ],
)
def test_protocol_compatibility(client: TestClient, signup, header: str | None, status: int, code: str | None) -> None:
    token, _ = signup()
    headers = auth(token)
    if header is None:
        client.headers.pop(PROTOCOL_HEADER, None)
    else:
        headers[PROTOCOL_HEADER] = header
    response = client.get("/v1/me", headers=headers)
    assert response.status_code == status
    assert response.headers[PROTOCOL_HEADER] == PROTOCOL_VERSION
    if code:
        assert (
            response.json()["detail"]["code"] == code
            and "pip install -U highhxcli" in response.json()["detail"]["hint"]
        )


def test_protocol_exempt_endpoints(client: TestClient) -> None:
    client.headers.pop(PROTOCOL_HEADER, None)
    assert client.get("/v1/meta").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").json()["status"] == "ok"


# ------------------------------------------------------------ streaming/resume
def test_events_have_sequential_ids(client: TestClient, signup, upstream: Upstream) -> None:
    token, _ = signup(plan="pro")
    upstream.responses.append([TextDelta("a"), TextDelta("b"), DONE])
    with client.stream(
        "POST", "/v1/ai/messages", json=body(), headers={**auth(token), "Idempotency-Key": "key-0000001"}
    ) as r:
        events = read_events(r)
    assert [e.id for e in events] == [1, 2, 3]
    assert [e.event for e in events] == ["text", "text", "completed"]


def _live_signup(server: LiveServer, *, pro: bool) -> str:
    email = f"live{time.monotonic_ns()}@example.com"
    status, data = server.json("POST", "/v1/auth/signup", {"email": email, "password": "correct horse battery"})
    assert status == 201, data
    if pro:
        server.json("POST", "/v1/admin/plan", {"email": email, "plan": "pro"}, {"x-admin-token": "admin-secret"})
    return str(data["access_token"])


def test_resume_replays_missed_events_without_calling_the_model_again(settings: Settings) -> None:
    """Real TCP disconnect mid-stream, then reconnect with Last-Event-ID: only missed events,
    one upstream call, one usage record."""
    gated = GatedUpstream([TextDelta("a"), TextDelta("b"), TextDelta("c"), DONE])
    app = create_app(settings, provider_factory=gated.factory)
    with LiveServer(app) as server:
        token = _live_signup(server, pro=True)
        headers = {**auth(token), "Idempotency-Key": "resume-0001"}
        gated.release(2)
        first = server.stream(body(), headers, limit=2)
        assert [e.id for e in first] == [1, 2]
        gated.release()
        rest = server.stream(body(), {**headers, "Last-Event-ID": "2"})
        assert [e.id for e in rest] == [3, 4] and rest[-1].event == "completed"
        assert gated.calls == 1
        with app.state.db.sessions() as s:
            rows = list(s.scalars(select(UsageRecord)).all())
        assert len(rows) == 1 and rows[0].status == "ok" and rows[0].input_tokens == 300
    app.state.db.engine.dispose()


def test_duplicate_request_with_same_key_attaches_instead_of_recharging(settings: Settings, signup_factory) -> None:
    gated = GatedUpstream([TextDelta("a"), DONE])
    with gated_client(settings, gated) as client:
        token = signup_factory(client, plan="pro")
        headers = {**auth(token), "Idempotency-Key": "dup-000001"}
        results: list[list[sse.ServerEvent]] = []

        def call() -> None:
            with client.stream("POST", "/v1/ai/messages", json=body(), headers=headers) as r:
                results.append(read_events(r))

        threads = [threading.Thread(target=call) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        gated.release()
        for t in threads:
            t.join(10)
        assert gated.calls == 1
        assert len(results) == 3 and all(r and r[-1].event == "completed" for r in results)
        assert len(usage_rows(client)) == 1


def test_resume_of_unknown_stream_and_replay_after_finish(client: TestClient, signup, upstream: Upstream) -> None:
    token, _ = signup(plan="pro")
    gone = client.post(
        "/v1/ai/messages", json=body(), headers={**auth(token), "Idempotency-Key": "never-001", "Last-Event-ID": "3"}
    )
    assert gone.status_code == 410 and gone.json()["detail"]["code"] == "stream_expired"
    upstream.responses.append(text_reply("x"))
    headers = {**auth(token), "Idempotency-Key": "finished-01"}
    with client.stream("POST", "/v1/ai/messages", json=body(), headers=headers) as r:
        read_events(r)
    with client.stream("POST", "/v1/ai/messages", json=body(), headers=headers) as r:  # recently finished: replay
        replay = read_events(r)
    assert replay[-1].event == "completed" and len(upstream.requests) == 1
    client.app.state.streams.retention = 0
    client.app.state.streams.prune(force=True)
    again = client.post("/v1/ai/messages", json=body(), headers=headers)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "duplicate_request"


def test_cancel_endpoint_stops_the_upstream_and_is_idempotent(settings: Settings, signup_factory) -> None:
    gated = GatedUpstream([TextDelta("a"), TextDelta("b"), DONE])
    with gated_client(settings, gated) as client:
        token = signup_factory(client, plan="pro")
        headers = {**auth(token), "Idempotency-Key": "cancel-001"}
        gated.release(1)
        collected: list[sse.ServerEvent] = []

        def consume() -> None:
            with client.stream("POST", "/v1/ai/messages", json=body(), headers=headers) as r:
                collected.extend(read_events(r))

        reader = threading.Thread(target=consume)
        reader.start()
        time.sleep(0.4)
        first = client.post("/v1/ai/messages/cancel-001/cancel", headers=auth(token)).json()
        second = client.post("/v1/ai/messages/cancel-001/cancel", headers=auth(token)).json()
        reader.join(10)
        assert first["cancelled"] and second["cancelled"]
        assert gated.cancelled.wait(5)
        assert collected[-1].event == "error" and collected[-1].data["code"] == "cancelled"
        time.sleep(0.2)
        assert usage_rows(client)[0].status == "cancelled"


def test_abandoned_stream_is_cancelled_after_the_grace_period(settings: Settings) -> None:
    """A client that disconnects and never comes back must not leave a zombie upstream call."""
    settings.stream_resume_grace = 0.3
    gated = GatedUpstream([TextDelta("a"), TextDelta("b"), DONE])
    app = create_app(settings, provider_factory=gated.factory)
    with LiveServer(app) as server:
        token = _live_signup(server, pro=True)
        gated.release(1)
        server.stream(body(), {**auth(token), "Idempotency-Key": "abandon-01"}, limit=1)
        assert gated.cancelled.wait(10), "the upstream call kept running without a client"
        deadline = time.monotonic() + 5
        status = ""
        while time.monotonic() < deadline and status != "cancelled":
            with app.state.db.sessions() as s:
                status = s.scalars(select(UsageRecord.status)).one()
            time.sleep(0.05)
        assert status == "cancelled"
    app.state.db.engine.dispose()


def test_concurrency_limit(settings: Settings, signup_factory) -> None:
    settings.max_concurrent_streams = 1
    gated = GatedUpstream([TextDelta("a"), DONE])
    with gated_client(settings, gated) as client:
        token = signup_factory(client, plan="pro")

        def hold() -> None:
            with client.stream(
                "POST", "/v1/ai/messages", json=body(), headers={**auth(token), "Idempotency-Key": "conc-0001"}
            ) as r:
                read_events(r)

        reader = threading.Thread(target=hold)
        reader.start()
        time.sleep(0.3)
        blocked = client.post("/v1/ai/messages", json=body(), headers={**auth(token), "Idempotency-Key": "conc-0002"})
        assert blocked.status_code == 429 and blocked.json()["detail"]["code"] == "too_many_concurrent"
        gated.release()
        reader.join(10)


def test_failed_and_retried_calls_are_metered_per_attempt(client: TestClient, signup, upstream: Upstream) -> None:
    from highhx.core.errors import ModelProviderError

    token, _ = signup(plan="pro")
    upstream.responses += [ModelProviderError("overloaded", retryable=True), text_reply("ok")]
    for key in ("attempt-f01", "attempt-f02"):  # the CLI uses a new key for each retry attempt
        with client.stream(
            "POST", "/v1/ai/messages", json=body(), headers={**auth(token), "Idempotency-Key": key}
        ) as r:
            read_events(r)
    rows = usage_rows(client)
    assert [r.status for r in rows] == ["error", "ok"]
    assert rows[0].input_tokens == 0 and rows[1].input_tokens == 1000
    assert client.get("/v1/usage", headers=auth(token)).json()["tokens_used"] == 1200


def test_fallback_provider_used_only_before_output(settings: Settings, signup_factory) -> None:
    from highhx.core.errors import ModelProviderError

    settings.fallback_providers = ("openai",)
    calls: list[str] = []

    def factory(name: str, key: str) -> Any:
        class P:
            default_model = "x"

            def stream(self, request: Any, *, cancel: Any = None) -> Any:
                calls.append(name)
                if name == "anthropic":
                    raise ModelProviderError("down", retryable=True)
                yield from text_reply("from openai", model="gpt-5")

        return P()

    with TestClient(
        create_app(settings, provider_factory=factory), headers={PROTOCOL_HEADER: PROTOCOL_VERSION}
    ) as client:
        token = signup_factory(client, plan="pro")
        with client.stream(
            "POST", "/v1/ai/messages", json=body(), headers={**auth(token), "Idempotency-Key": "fallback-1"}
        ) as r:
            events = read_events(r)
        assert calls == ["anthropic", "openai"] and events[-1].event == "completed"
        assert usage_rows(client)[0].provider == "openai"


# ------------------------------------------------------------- entitlements
@pytest.mark.parametrize("plan", ["free", "pro"])
def test_entitlement_matrix(client: TestClient, signup, upstream: Upstream, plan: str) -> None:
    token, _ = signup(plan=plan if plan == "pro" else None)
    headers = auth(token)
    # Deterministic / account features: allowed on both plans.
    assert client.get("/v1/me", headers=headers).status_code == 200
    assert client.get("/v1/usage", headers=headers).status_code == 200
    ai = client.post("/v1/ai/messages", json=body(), headers={**headers, "Idempotency-Key": f"ent-{plan}-ai01"})
    computer = client.post(
        "/v1/ai/messages",
        json=body(tools=[{"name": "computer_act", "description": "", "parameters": {"type": "object"}}]),
        headers={**headers, "Idempotency-Key": f"ent-{plan}-cu01"},
    )
    if plan == "free":
        assert ai.status_code == 402 and computer.status_code == 402
    else:
        assert ai.status_code == 200 and computer.status_code == 200


def test_free_user_cannot_unlock_pro_from_the_client(client: TestClient, signup) -> None:
    token, account = signup()
    headers = auth(token)
    # Claims in the request body, headers and query are ignored.
    forged = client.post(
        "/v1/ai/messages?plan=pro",
        json={**body(), "plan": "pro", "features": ["agent"]},
        headers={**headers, "X-HighhX-Plan": "pro", "Idempotency-Key": "forge-0001"},
    )
    assert forged.status_code in (402, 422)
    assert client.patch("/v1/me/settings", json={"plan": "pro"}, headers=headers).status_code == 400
    assert client.patch("/v1/me/settings", json={"agent": {"plan": "pro"}}, headers=headers).status_code == 400
    # Internal / support endpoints need the admin token.
    assert (
        client.post(
            "/v1/admin/plan", json={"email": account["user"]["email"], "plan": "pro"}, headers=headers
        ).status_code
        == 403
    )
    # A forged webhook is rejected.
    assert (
        client.post("/v1/billing/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=00"}).status_code == 400
    )
    assert client.get("/v1/me", headers=headers).json()["plan"] == "free"


def test_downgrade_takes_effect_immediately_even_for_old_requests(
    client: TestClient, signup, upstream: Upstream
) -> None:
    token, account = signup(plan="pro")
    headers = {**auth(token), "Idempotency-Key": "replay-001"}
    with client.stream("POST", "/v1/ai/messages", json=body(), headers=headers) as r:
        read_events(r)
    client.post(
        "/v1/admin/plan",
        json={"email": account["user"]["email"], "plan": "free"},
        headers={"x-admin-token": "admin-secret"},
    )
    replay = client.post("/v1/ai/messages", json=body(), headers=headers)
    assert replay.status_code == 402


def _subscribe(client: TestClient, user_id: str, *, status: str, period_end: Any) -> None:
    with db_session(client) as s:
        s.add(Subscription(user_id=user_id, plan="pro", status=status, current_period_end=period_end))
        s.execute(update(User).where(User.id == user_id).values(plan="pro" if status == "active" else "free"))
        s.commit()


@pytest.mark.parametrize(
    ("status", "offset_days", "expected"),
    [
        ("active", 20, "pro"),
        ("past_due", 2, "pro"),
        ("active", -10, "free"),
        ("canceled", 20, "free"),
        ("unpaid", 20, "free"),
    ],
)
def test_subscription_state_decides_the_plan(
    client: TestClient, signup, status: str, offset_days: int, expected: str
) -> None:
    token, account = signup()
    _subscribe(client, account["user"]["id"], status=status, period_end=utcnow() + timedelta(days=offset_days))
    assert client.get("/v1/me", headers=auth(token)).json()["plan"] == expected


# ------------------------------------------------------------ auth lifecycle
def test_token_expiry_refresh_and_revocation(client: TestClient, signup) -> None:
    token, _account = signup()
    refreshed = client.post("/v1/auth/refresh", headers=auth(token)).json()["access_token"]
    assert client.get("/v1/me", headers=auth(token)).status_code == 401  # old token revoked by rotation
    assert client.get("/v1/me", headers=auth(refreshed)).status_code == 200
    with db_session(client) as s:
        s.execute(update(ApiToken).values(expires_at=utcnow() - timedelta(seconds=1)))
        s.commit()
    assert client.get("/v1/me", headers=auth(refreshed)).status_code == 401  # stale token


def test_suspended_accounts_are_locked_out(client: TestClient, signup) -> None:
    token, account = signup(plan="pro")
    email = account["user"]["email"]
    admin = {"x-admin-token": "admin-secret"}
    assert client.post("/v1/admin/suspend", json={"email": email}, headers=admin).status_code == 200
    denied = client.get("/v1/me", headers=auth(token))
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "account_suspended"
    assert client.post("/v1/auth/login", json={"email": email, "password": "correct horse battery"}).status_code == 403
    client.post("/v1/admin/suspend", json={"email": email, "suspended": False}, headers=admin)
    assert client.get("/v1/me", headers=auth(token)).status_code == 200
    with db_session(client) as s:
        actions = [e.action for e in s.scalars(select(AuditEvent).order_by(AuditEvent.created_at))]
    assert actions[-2:] == ["suspend", "reinstate"]


def test_concurrent_sessions_are_independent(client: TestClient, signup) -> None:
    token, account = signup()
    second = client.post(
        "/v1/auth/login", json={"email": account["user"]["email"], "password": "correct horse battery"}
    ).json()["access_token"]
    client.post("/v1/auth/logout", headers=auth(token))
    assert client.get("/v1/me", headers=auth(token)).status_code == 401
    assert client.get("/v1/me", headers=auth(second)).status_code == 200


# ------------------------------------------------------------------- billing
def _signed(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload).encode()
    ts = int(time.time())
    sig = hmac.new(b"whsec_test", f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"stripe-signature": f"t={ts},v1={sig}"}


def test_out_of_order_webhooks_do_not_regress_state(client: TestClient, signup) -> None:
    token, account = signup()
    uid = account["user"]["id"]
    now = int(time.time())
    sub = {
        "id": "sub_9",
        "customer": "cus_9",
        "metadata": {"plan": "pro", "user_id": uid},
        "current_period_end": now + 86400 * 30,
    }
    newer = {
        "id": "evt_new",
        "type": "customer.subscription.deleted",
        "created": now,
        "data": {"object": {**sub, "status": "canceled"}},
    }
    older = {
        "id": "evt_old",
        "type": "customer.subscription.updated",
        "created": now - 60,
        "data": {"object": {**sub, "status": "active"}},
    }
    for event in (newer, older):
        raw, headers = _signed(event)
        client.post("/v1/billing/webhook", content=raw, headers=headers)
    me = client.get("/v1/me", headers=auth(token)).json()
    assert me["plan"] == "free" and me["subscription"]["status"] == "canceled"


def test_renewal_and_failed_payment(client: TestClient, signup) -> None:
    token, account = signup()
    uid = account["user"]["id"]
    now = int(time.time())
    base = {"id": "sub_r", "customer": "cus_r", "metadata": {"plan": "pro", "user_id": uid}}
    events = [
        {
            "id": "e1",
            "type": "customer.subscription.created",
            "created": now - 30,
            "data": {"object": {**base, "status": "active", "current_period_end": now - 3600 * 24 * 5}},
        },
        {
            "id": "e2",
            "type": "invoice.paid",
            "created": now - 20,
            "data": {
                "object": {
                    "customer": "cus_r",
                    "subscription": "sub_r",
                    "lines": {"data": [{"period": {"start": now, "end": now + 86400 * 30}}]},
                }
            },
        },
        {
            "id": "e3",
            "type": "invoice.payment_failed",
            "created": now - 10,
            "data": {"object": {"customer": "cus_r", "subscription": "sub_r"}},
        },
    ]
    raw, headers = _signed(events[0])
    client.post("/v1/billing/webhook", content=raw, headers=headers)
    assert client.get("/v1/me", headers=auth(token)).json()["plan"] == "free"  # its paid period is over
    raw, headers = _signed(events[1])
    assert client.post("/v1/billing/webhook", content=raw, headers=headers).json()["outcome"] == "renewed"
    assert client.get("/v1/me", headers=auth(token)).json()["plan"] == "pro"
    raw, headers = _signed(events[2])
    client.post("/v1/billing/webhook", content=raw, headers=headers)
    me = client.get("/v1/me", headers=auth(token)).json()
    assert me["subscription"]["status"] == "past_due" and me["plan"] == "pro"  # Stripe is retrying the payment


def test_concurrent_duplicate_webhooks_apply_once(settings: Settings) -> None:
    with TestClient(
        create_app(settings, provider_factory=Upstream().factory), headers={PROTOCOL_HEADER: PROTOCOL_VERSION}
    ) as client:
        token = client.post(
            "/v1/auth/signup", json={"email": "w@example.com", "password": "correct horse battery"}
        ).json()["access_token"]
        uid = client.get("/v1/me", headers=auth(token)).json()["user"]["id"]
        event = {
            "id": "evt_concurrent",
            "type": "customer.subscription.created",
            "created": int(time.time()),
            "data": {
                "object": {
                    "id": "sub_c",
                    "customer": "cus_c",
                    "status": "active",
                    "metadata": {"plan": "pro", "user_id": uid},
                }
            },
        }
        raw, headers = _signed(event)
        outcomes: list[str] = []
        barrier = threading.Barrier(4)

        def deliver() -> None:
            barrier.wait()
            outcomes.append(
                client.post("/v1/billing/webhook", content=raw, headers=headers).json().get("outcome", "error")
            )

        threads = [threading.Thread(target=deliver) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert outcomes.count("subscription active") == 1 and outcomes.count("duplicate") == 3
        with db_session(client) as s:
            assert len(s.scalars(select(Subscription)).all()) == 1


# ------------------------------------------------------------------ isolation
def test_users_cannot_touch_each_others_resources(client: TestClient, signup, upstream: Upstream) -> None:
    a, _ = signup(plan="pro")
    b, _ = signup(plan="pro")
    session = client.post("/v1/agent/sessions", json={"title": "A's work"}, headers=auth(a)).json()
    project = client.post("/v1/projects", json={"name": "a", "fingerprint": "aaaaaaaaaaaa"}, headers=auth(a)).json()
    token_id = client.get("/v1/tokens", headers=auth(a)).json()["items"][0]["id"]
    assert client.patch(f"/v1/agent/sessions/{session['id']}", json={"title": "x"}, headers=auth(b)).status_code == 404
    assert client.get("/v1/agent/sessions", headers=auth(b)).json()["items"] == []
    assert all(p["id"] != project["id"] for p in client.get("/v1/projects", headers=auth(b)).json()["items"])
    assert client.delete(f"/v1/tokens/{token_id}", headers=auth(b)).status_code == 404
    upstream.responses.append(text_reply("a only"))
    with client.stream(
        "POST", "/v1/ai/messages", json=body(), headers={**auth(a), "Idempotency-Key": "iso-000001"}
    ) as r:
        read_events(r)
    # B cannot resume, replay or cancel A's stream (keys are scoped to the account).
    assert client.post("/v1/ai/messages/iso-000001/cancel", headers=auth(b)).json() == {
        "cancelled": False,
        "status": "unknown",
    }
    other = client.post(
        "/v1/ai/messages", json=body(), headers={**auth(b), "Idempotency-Key": "iso-000001", "Last-Event-ID": "1"}
    )
    assert other.status_code == 410
    assert client.get("/v1/usage", headers=auth(b)).json()["requests"] == 0
    assert client.get("/v1/usage", headers=auth(a)).json()["requests"] == 1


# ----------------------------------------------------------- session lifecycle
def test_session_transitions_are_validated(client: TestClient, signup) -> None:
    token, _ = signup(plan="pro")
    sid = client.post("/v1/agent/sessions", json={}, headers=auth(token)).json()["id"]

    def patch(status: str) -> int:
        return int(client.patch(f"/v1/agent/sessions/{sid}", json={"status": status}, headers=auth(token)).status_code)

    assert patch("completed") == 409  # created → completed is not allowed
    assert patch("running") == 200
    assert patch("waiting_for_confirmation") == 200
    assert patch("completed") == 409  # must resume running first
    assert patch("running") == 200
    assert patch("cancelled") == 200
    assert patch("closed") == 200
    assert patch("waiting_for_confirmation") == 409
    assert patch("nonsense") == 400


def test_concurrent_transitions_only_one_wins(client: TestClient, signup) -> None:
    token, _ = signup(plan="pro")
    sid = client.post("/v1/agent/sessions", json={}, headers=auth(token)).json()["id"]
    client.patch(f"/v1/agent/sessions/{sid}", json={"status": "running"}, headers=auth(token))
    results: list[int] = []
    barrier = threading.Barrier(2)

    def move(target: str) -> None:
        barrier.wait()
        results.append(
            client.patch(f"/v1/agent/sessions/{sid}", json={"status": target}, headers=auth(token)).status_code
        )

    threads = [threading.Thread(target=move, args=(t,)) for t in ("completed", "waiting_for_confirmation")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    final = client.get("/v1/agent/sessions", headers=auth(token)).json()["items"][0]["status"]
    assert sorted(results) in ([200, 200], [200, 409])
    assert final in ("completed", "waiting_for_confirmation")


# ---------------------------------------------------------------- migrations
def test_models_match_migrations(settings: Settings) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from highhx_platform.db import Base, Database

    database = Database(settings.database_url)
    database.migrate()
    with database.engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    database.engine.dispose()
    assert diff == []


def test_upgrade_from_a_populated_0_1_0_database(settings: Settings) -> None:
    from sqlalchemy import text

    from highhx_platform.db import Database

    database = Database(settings.database_url)
    database.migrate("0001")
    with database.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, name, password_hash, plan, settings, is_active, created_at)"
                " VALUES ('u1', 'old@example.com', '', 'scrypt$x$y', 'pro', '{}', true, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO usage_records (id, user_id, session_id, provider, model, input_tokens, output_tokens,"
                " cache_read_tokens, status, created_at) VALUES ('r1', 'u1', 's', 'anthropic', 'm', 5, 6, 0, 'ok', CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(text("DROP TABLE alembic_version"))  # 0.1.0 had no migration history
    database.migrate()
    assert database.schema_revision() == database.head_revision() == "0003"
    with database.sessions() as s:
        user = s.get(User, "u1")
        assert user is not None and user.email == "old@example.com" and user.suspended_at is None
        record = s.get(UsageRecord, "r1")
        assert record is not None and record.request_id is None and record.input_tokens == 5
    database.engine.dispose()
