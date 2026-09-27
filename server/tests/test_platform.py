"""Platform API: accounts, device sign-in, entitlements, AI gateway, usage, sessions, billing."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from helpers import PASSWORD, Upstream, auth, text_reply, tool_reply
from highhx.agent.messages import Message, Usage
from highhx.cloud import sse
from highhx.core.errors import ModelProviderError
from highhx_platform import security
from highhx_platform.billing import BillingError, verify_signature


def gateway_body(**extra: Any) -> dict[str, Any]:
    return {"system": "You are HighhX.", "messages": [Message.user("hello").to_dict()], "tools": [], **extra}


def stream(client: TestClient, token: str, body: dict[str, Any]) -> tuple[int, list[sse.ServerEvent], dict[str, Any]]:
    with client.stream("POST", "/v1/ai/messages", json=body, headers=auth(token)) as response:
        if response.status_code != 200:
            response.read()
            return response.status_code, [], response.json()
        events = list(sse.decode(response.iter_lines()))
        return 200, events, dict(response.headers)


# ------------------------------------------------------------------- accounts
def test_signup_login_and_me(client: TestClient, signup) -> None:
    token, account = signup("Dev@Example.com")
    assert token.startswith("hhx_") and account["user"]["email"] == "dev@example.com"
    assert account["plan"] == "free" and "agent" not in account["features"]
    login = client.post("/v1/auth/login", json={"email": "dev@example.com", "password": PASSWORD})
    assert login.status_code == 200
    me = client.get("/v1/me", headers=auth(login.json()["access_token"])).json()
    assert me["user"]["email"] == "dev@example.com"
    assert (
        client.post("/v1/auth/login", json={"email": "dev@example.com", "password": "wrong password!"}).status_code
        == 401
    )


def test_signup_validation(client: TestClient, signup) -> None:
    assert (
        client.post("/v1/auth/signup", json={"email": "x@y.z", "password": "short"}).json()["detail"]["code"]
        == "weak_password"
    )
    signup("taken@example.com")
    again = client.post("/v1/auth/signup", json={"email": "taken@example.com", "password": PASSWORD})
    assert again.status_code == 409


def test_unauthenticated_and_revoked_tokens(client: TestClient, signup) -> None:
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers=auth("hhx_bogus")).status_code == 401
    token, _ = signup()
    assert client.post("/v1/auth/logout", headers=auth(token)).status_code == 200
    denied = client.get("/v1/me", headers=auth(token))
    assert denied.status_code == 401 and denied.json()["detail"]["hint"] == "Run `highhx login`."


def test_tokens_are_stored_hashed(client: TestClient, signup) -> None:
    token, _ = signup()
    created = client.post("/v1/tokens", json={"name": "ci"}, headers=auth(token)).json()
    listing = client.get("/v1/tokens", headers=auth(token)).json()["items"]
    assert {t["name"] for t in listing} >= {"api", "ci"}
    assert all(created["access_token"] not in json.dumps(t) for t in listing)
    ci = next(t for t in listing if t["name"] == "ci")
    assert client.delete(f"/v1/tokens/{ci['id']}", headers=auth(token)).status_code == 200
    assert client.get("/v1/me", headers=auth(created["access_token"])).status_code == 401


def test_login_is_rate_limited(client: TestClient, signup) -> None:
    signup("limited@example.com")
    codes = [
        client.post("/v1/auth/login", json={"email": "limited@example.com", "password": "nope-nope-nope"}).status_code
        for _ in range(12)
    ]
    assert codes[-1] == 429 and 401 in codes


def test_password_hashing() -> None:
    stored = security.hash_password("s3cret-passphrase")
    assert stored.startswith("scrypt$") and "s3cret" not in stored
    assert security.verify_password("s3cret-passphrase", stored)
    assert not security.verify_password("other", stored)
    assert not security.verify_password("x", "md5$abc")


# ---------------------------------------------------------------- device flow
def test_device_flow_with_account_creation(client: TestClient) -> None:
    start = client.post(
        "/v1/auth/device", json={"client": "highhx-cli", "version": "0.1.0", "hostname": "laptop"}
    ).json()
    assert start["verification_uri"] == "https://platform.test/device"
    assert client.post("/v1/auth/device/token", json={"device_code": start["device_code"]}).json() == {
        "status": "pending"
    }
    page = client.get(f"/device?code={start['user_code']}")
    assert page.status_code == 200 and "laptop" in page.text and page.headers["x-frame-options"] == "DENY"
    form = f"code={start['user_code'].lower().replace('-', '')}&email=new%40example.com&password=correct+horse+battery&create=1&name=New&action=approve"
    approved = client.post("/device", content=form, headers={"content-type": "application/x-www-form-urlencoded"})
    assert approved.status_code == 200 and "signed in" in approved.text
    token = client.post("/v1/auth/device/token", json={"device_code": start["device_code"]}).json()
    assert token["status"] == "approved"
    assert client.get("/v1/me", headers=auth(token["access_token"])).json()["user"]["email"] == "new@example.com"
    # The code works exactly once.
    assert client.post("/v1/auth/device/token", json={"device_code": start["device_code"]}).json() == {
        "status": "expired"
    }


def test_device_flow_wrong_password_and_deny(client: TestClient, signup) -> None:
    signup("known@example.com")
    start = client.post("/v1/auth/device", json={}).json()
    headers = {"content-type": "application/x-www-form-urlencoded"}
    bad = client.post(
        "/device",
        content=f"code={start['user_code']}&email=known%40example.com&password=wrongwrongwrong&action=approve",
        headers=headers,
    )
    assert bad.status_code == 400 and "Wrong email or password" in bad.text
    client.post("/device", content=f"code={start['user_code']}&action=deny", headers=headers)
    assert client.post("/v1/auth/device/token", json={"device_code": start["device_code"]}).json() == {
        "status": "denied"
    }


# ------------------------------------------------------------------ gateway
def test_free_plan_cannot_use_the_gateway(client: TestClient, signup) -> None:
    token, _ = signup()
    status, _events, body = stream(client, token, gateway_body())
    assert status == 402 and body["detail"]["code"] == "plan_required"


def test_gateway_streams_and_meters_usage(client: TestClient, signup, upstream: Upstream) -> None:
    token, _ = signup(plan="pro")
    upstream.responses.append(text_reply("Hello from Claude"))
    status, events, headers = stream(client, token, gateway_body(max_tokens=128_000))
    assert status == 200 and headers["x-highhx-provider"] == "anthropic"
    assert [e.event for e in events] == ["text", "completed"]
    assert events[-1].data["message"]["blocks"][0]["text"] == "Hello from Claude"
    name, request = upstream.requests[0]
    assert name == "anthropic" and request.model == "claude-opus-5" and request.max_tokens == 64_000
    usage = client.get("/v1/usage", headers=auth(token)).json()
    assert usage["tokens_used"] == 1200 and usage["requests"] == 1
    assert usage["by_model"][0] == {
        "provider": "anthropic",
        "model": "claude-opus-5",
        "requests": 1,
        "input_tokens": 1000,
        "output_tokens": 200,
    }


def test_gateway_routes_by_model_and_account_upstream(client: TestClient, signup, upstream: Upstream) -> None:
    token, _ = signup(plan="pro")
    stream(client, token, gateway_body(model="gpt-5"))
    assert upstream.requests[-1][0] == "openai"
    client.patch("/v1/me/settings", json={"agent": {"upstream": "openai"}}, headers=auth(token))
    stream(client, token, gateway_body())
    assert upstream.requests[-1] == ("openai", upstream.requests[-1][1]) and upstream.requests[-1][1].model == "gpt-5"
    status, _e, body = stream(client, token, gateway_body(model="gemini-2.5-pro"))
    assert status == 503 and body["detail"]["code"] == "provider_unavailable"
    status, _e, body = stream(client, token, gateway_body(model="not-a-model"))
    assert status == 400 and body["detail"]["code"] == "invalid_model"


def test_gateway_forwards_tools_and_errors(client: TestClient, signup, upstream: Upstream) -> None:
    token, _ = signup(plan="pro")
    upstream.responses += [
        tool_reply("run_tests", {}),
        ModelProviderError("Anthropic rate limit reached.", retryable=True),
    ]
    tools = [{"name": "run_tests", "description": "Run tests", "parameters": {"type": "object"}}]
    _s, events, _h = stream(client, token, gateway_body(tools=tools))
    assert events[-1].data["stop_reason"] == "tool_use"
    assert upstream.requests[-1][1].tools[0].name == "run_tests"
    _s, events, _h = stream(client, token, gateway_body())
    assert events == [
        sse.ServerEvent(
            "error",
            {"code": "upstream_error", "message": "Anthropic rate limit reached.", "hint": None, "retryable": True},
            id=1,
        )
    ]
    assert client.get("/v1/usage", headers=auth(token)).json()["requests"] == 2


def test_quota_is_enforced(client: TestClient, signup, upstream: Upstream, settings) -> None:
    token, _ = signup(plan="pro")
    upstream.responses.append(text_reply("big", usage=Usage(20_000_000, 1)))
    stream(client, token, gateway_body())
    status, _e, body = stream(client, token, gateway_body())
    assert status == 429 and body["detail"]["code"] == "quota_exceeded"


def test_gateway_rejects_malformed_requests(client: TestClient, signup) -> None:
    token, _ = signup(plan="pro")
    bad = client.post("/v1/ai/messages", json={"messages": [{"role": "robot"}]}, headers=auth(token))
    assert bad.status_code == 400
    assert client.post("/v1/ai/messages", json={"messages": []}, headers=auth(token)).status_code == 422


# ----------------------------------------------------------- settings/projects
def test_settings_are_validated(client: TestClient, signup) -> None:
    token, _ = signup()
    ok = client.patch(
        "/v1/me/settings",
        json={"agent": {"provider": "openai", "model": "gpt-5", "sync_sessions": False}},
        headers=auth(token),
    )
    assert ok.json() == {"agent": {"provider": "openai", "model": "gpt-5", "sync_sessions": False}}
    assert (
        client.patch("/v1/me/settings", json={"agent": {"provider": "skynet"}}, headers=auth(token)).status_code == 400
    )
    assert client.patch("/v1/me/settings", json={"theme": "dark"}, headers=auth(token)).status_code == 400
    cleared = client.patch("/v1/me/settings", json={"agent": {"model": None}}, headers=auth(token)).json()
    assert "model" not in cleared["agent"]


def test_projects_and_sessions(client: TestClient, signup) -> None:
    token, _ = signup(plan="pro")
    project = client.post(
        "/v1/projects", json={"name": "api", "fingerprint": "abc123def456", "stack": "Python"}, headers=auth(token)
    ).json()
    again = client.post(
        "/v1/projects", json={"name": "api-renamed", "fingerprint": "abc123def456"}, headers=auth(token)
    ).json()
    assert project["id"] == again["id"] and again["name"] == "api-renamed"
    created = client.post(
        "/v1/agent/sessions",
        json={"project_id": project["id"], "title": "fix tests", "provider": "highhx"},
        headers=auth(token),
    )
    assert created.status_code == 201
    session_id = created.json()["id"]
    patched = client.patch(
        f"/v1/agent/sessions/{session_id}",
        json={"turns": 3, "status": "closed", "usage": {"input_tokens": 9}},
        headers=auth(token),
    )
    assert patched.json()["turns"] == 3 and patched.json()["usage"] == {"input_tokens": 9}
    other, _ = signup(plan="pro")
    assert client.patch(f"/v1/agent/sessions/{session_id}", json={"turns": 1}, headers=auth(other)).status_code == 404
    assert [s["id"] for s in client.get("/v1/agent/sessions", headers=auth(token)).json()["items"]] == [session_id]
    free, _ = signup()
    assert client.post("/v1/agent/sessions", json={}, headers=auth(free)).status_code == 402


# ------------------------------------------------------------------- billing
def signed(payload: dict[str, Any], secret: str = "whsec_test", *, timestamp: int | None = None) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    ts = timestamp or int(time.time())
    signature = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={signature}"


def test_webhook_signature_verification() -> None:
    body, header = signed({"id": "evt"})
    verify_signature(body, header, "whsec_test")
    with pytest.raises(BillingError, match="signature"):
        verify_signature(body, header, "whsec_other")
    old_body, old_header = signed({"id": "evt"}, timestamp=int(time.time()) - 3600)
    with pytest.raises(BillingError, match="tolerance"):
        verify_signature(old_body, old_header, "whsec_test")


def test_subscription_lifecycle_via_webhooks(client: TestClient, signup) -> None:
    token, account = signup()
    user_id = account["user"]["id"]
    now = int(time.time())
    subscription = {
        "id": "sub_1",
        "status": "active",
        "current_period_start": now,
        "current_period_end": now + 30 * 86400,
        "metadata": {"plan": "pro", "user_id": user_id},
        "customer": "cus_1",
    }

    class FakeStripe:
        def get(self, path: str) -> dict[str, Any]:
            assert path == "/subscriptions/sub_1"
            return subscription

        def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("the webhook handler must not modify Stripe objects")

    client.app.state.stripe_factory = lambda settings: FakeStripe()
    body, header = signed(
        {
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {"object": {"client_reference_id": user_id, "customer": "cus_1", "subscription": "sub_1"}},
        }
    )
    result = client.post("/v1/billing/webhook", content=body, headers={"stripe-signature": header})
    assert result.json() == {"received": True, "outcome": "activated"}
    me = client.get("/v1/me", headers=auth(token)).json()
    assert me["plan"] == "pro" and "agent" in me["features"] and me["subscription"]["status"] == "active"
    # Replays are ignored.
    assert (
        client.post("/v1/billing/webhook", content=body, headers={"stripe-signature": header}).json()["outcome"]
        == "duplicate"
    )
    cancelled, header = signed(
        {
            "id": "evt_2",
            "type": "customer.subscription.deleted",
            "data": {"object": {**subscription, "status": "canceled"}},
        }
    )
    client.post("/v1/billing/webhook", content=cancelled, headers={"stripe-signature": header})
    me = client.get("/v1/me", headers=auth(token)).json()
    assert me["plan"] == "free" and "agent" not in me["features"]
    forged, _ = signed({"id": "evt_3", "type": "checkout.session.completed", "data": {"object": {}}})
    assert (
        client.post("/v1/billing/webhook", content=forged, headers={"stripe-signature": "t=1,v1=00"}).status_code == 400
    )


def test_checkout_and_portal(client: TestClient, signup) -> None:
    token, _ = signup()
    posted: list[tuple[str, dict[str, Any]]] = []

    class FakeStripe:
        def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
            posted.append((path, data))
            return {"url": "https://checkout.stripe.test/c/1"}

    client.app.state.stripe_factory = lambda settings: FakeStripe()
    result = client.post("/v1/billing/checkout", json={"plan": "pro"}, headers=auth(token))
    assert result.json() == {"url": "https://checkout.stripe.test/c/1"}
    path, data = posted[0]
    assert (
        path == "/checkout/sessions"
        and data["line_items"][0]["price"] == "price_pro"
        and data["mode"] == "subscription"
    )
    assert client.post("/v1/billing/portal", headers=auth(token)).status_code == 404  # no customer yet
    pro, _ = signup(plan="pro")
    assert client.post("/v1/billing/checkout", json={}, headers=auth(pro)).status_code == 409


def test_admin_endpoint_requires_token(client: TestClient, signup) -> None:
    signup("a@example.com")
    assert client.post("/v1/admin/plan", json={"email": "a@example.com", "plan": "pro"}).status_code == 403
    assert (
        client.post(
            "/v1/admin/plan", json={"email": "a@example.com", "plan": "pro"}, headers={"x-admin-token": "wrong"}
        ).status_code
        == 403
    )


def test_meta_and_health(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    meta = client.get("/v1/meta").json()
    assert meta["providers"] == ["anthropic", "openai"] and meta["billing"] is True
