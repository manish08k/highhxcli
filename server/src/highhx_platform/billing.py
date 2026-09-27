"""Stripe integration: checkout, billing portal and signature-verified webhooks.

Talks to Stripe's REST API with the standard library (form-encoded requests),
so the platform does not depend on a payment SDK.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from highhx.cloud.plans import FREE, PRO
from highhx_platform.accounts import ACTIVE_STATUSES, record_event
from highhx_platform.config import Settings
from highhx_platform.models import Subscription, User, WebhookEvent

log = logging.getLogger(__name__)

STRIPE_API = "https://api.stripe.com/v1"
SIGNATURE_TOLERANCE = 300


class BillingError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _flatten(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            out += _flatten(value, name)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    out += _flatten(item, f"{name}[{index}]")
                else:
                    out.append((f"{name}[{index}]", str(item)))
        elif value is not None:
            out.append((name, str(value).lower() if isinstance(value, bool) else str(value)))
    return out


class StripeClient:
    def __init__(self, secret_key: str, *, base_url: str = STRIPE_API, timeout: float = 20.0) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("the payment provider URL must use https")
        self.secret_key = secret_key
        self.base_url = base_url
        self.timeout = timeout

    def get(self, path: str) -> dict[str, Any]:
        return self._send("GET", path, None)

    def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._send("POST", path, data)

    def _send(self, method: str, path: str, data: dict[str, Any] | None) -> dict[str, Any]:
        body = urllib.parse.urlencode(_flatten(data)).encode() if data is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.secret_key}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Stripe-Version": "2024-06-20",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310 - https enforced
                result: dict[str, Any] = json.loads(response.read())
                return result
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error", {}).get("message", "")
            except ValueError:
                message = ""
            raise BillingError(502, f"Payment provider error: {message or exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BillingError(502, f"Payment provider unreachable: {exc}") from None


def create_checkout(settings: Settings, client: StripeClient, user: User) -> str:
    data: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": settings.stripe_price_pro, "quantity": 1}],
        "success_url": f"{settings.public_url}/billing/success",
        "cancel_url": f"{settings.public_url}/billing/cancel",
        "client_reference_id": user.id,
        "metadata": {"user_id": user.id, "plan": PRO},
        "subscription_data": {"metadata": {"user_id": user.id, "plan": PRO}},
        "allow_promotion_codes": True,
    }
    if user.stripe_customer_id:
        data["customer"] = user.stripe_customer_id
    else:
        data["customer_email"] = user.email
    session = client.post("/checkout/sessions", data)
    url = session.get("url")
    if not isinstance(url, str):
        raise BillingError(502, "Payment provider did not return a checkout URL.")
    return url


def create_portal(settings: Settings, client: StripeClient, user: User) -> str:
    if not user.stripe_customer_id:
        raise BillingError(404, "No billing account yet — upgrade first with `highhx account upgrade`.")
    session = client.post(
        "/billing_portal/sessions",
        {"customer": user.stripe_customer_id, "return_url": f"{settings.public_url}/billing/success"},
    )
    url = session.get("url")
    if not isinstance(url, str):
        raise BillingError(502, "Payment provider did not return a portal URL.")
    return url


def verify_signature(payload: bytes, header: str, secret: str, *, now: float | None = None) -> None:
    """Stripe's scheme: ``t=<ts>,v1=<hex hmac-sha256 of "<ts>.<payload>">``."""
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError, IndexError):
        raise BillingError(400, "Missing webhook timestamp.") from None
    if abs((now or time.time()) - timestamp) > SIGNATURE_TOLERANCE:
        raise BillingError(400, "Webhook timestamp outside the tolerance window.")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise BillingError(400, "Invalid webhook signature.")


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if isinstance(value, int | float) else None


def _user_for(db: Session, obj: dict[str, Any]) -> User | None:
    metadata = obj.get("metadata") or {}
    user_id = obj.get("client_reference_id") or metadata.get("user_id")
    if user_id:
        user = db.get(User, str(user_id))
        if user is not None:
            return user
    customer = obj.get("customer")
    if customer:
        return db.scalar(select(User).where(User.stripe_customer_id == str(customer)))
    return None


def _apply_subscription(db: Session, user: User, sub: dict[str, Any], event_time: datetime | None) -> bool:
    """Apply Stripe subscription state. Returns False when the event is older than the state
    already applied (Stripe does not guarantee delivery order)."""
    record = user.subscription
    if record is not None and event_time is not None and record.last_event_at is not None:
        last = record.last_event_at if record.last_event_at.tzinfo else record.last_event_at.replace(tzinfo=UTC)
        if event_time < last:
            return False
    record = record or Subscription(user_id=user.id)
    if sub.get("customer") and not user.stripe_customer_id:
        user.stripe_customer_id = str(sub["customer"])  # later invoice events identify the user by customer
    record.stripe_subscription_id = str(sub.get("id") or record.stripe_subscription_id or "") or None
    record.status = str(sub.get("status") or record.status)
    record.plan = str((sub.get("metadata") or {}).get("plan") or PRO)
    record.current_period_start = _ts(sub.get("current_period_start")) or record.current_period_start
    record.current_period_end = _ts(sub.get("current_period_end")) or record.current_period_end
    record.cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    if event_time is not None:
        record.last_event_at = event_time
    if user.subscription is None:
        db.add(record)
        user.subscription = record
    user.plan = record.plan if record.status in ACTIVE_STATUSES else FREE
    return True


def _fetch_subscription(
    client: StripeClient | None, subscription_id: object, fallback: dict[str, Any]
) -> dict[str, Any]:
    if subscription_id and client is not None:
        try:
            return client.get(f"/subscriptions/{subscription_id}")
        except BillingError as exc:
            log.warning(
                "could not fetch subscription %s from Stripe (%s); using the event's data", subscription_id, exc.message
            )
    return fallback


def handle_event(db: Session, event: dict[str, Any], client: StripeClient | None) -> str:
    """Apply a verified webhook event. Idempotent by event id: the event row is inserted in the
    same transaction as its effects, so a concurrent duplicate delivery fails on the primary key
    and rolls back entirely (see the webhook route). Returns what happened."""
    event_id = str(event.get("id") or "")
    kind = str(event.get("type") or "")
    if not event_id:
        raise BillingError(400, "Event without id.")
    if db.get(WebhookEvent, event_id) is not None:
        return "duplicate"
    created = _ts(event.get("created"))
    record = WebhookEvent(id=event_id, type=kind, payload="", created=created)
    db.add(record)
    db.flush()
    obj = (event.get("data") or {}).get("object") or {}
    outcome = "ignored"
    user: User | None = None
    if kind == "checkout.session.completed":
        user = _user_for(db, obj)
        if user is not None:
            if obj.get("customer"):
                user.stripe_customer_id = str(obj["customer"])
            fallback = {"id": obj.get("subscription"), "status": "active", "metadata": {"plan": PRO}}
            sub = _fetch_subscription(client, obj.get("subscription"), fallback)
            outcome = "activated" if _apply_subscription(db, user, sub, created) else "stale"
    elif kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        user = _user_for(db, obj)
        if user is not None:
            if kind.endswith("deleted"):
                obj = {**obj, "status": "canceled"}
            applied = _apply_subscription(db, user, obj, created)
            outcome = f"subscription {obj.get('status')}" if applied else "stale"
    elif kind in ("invoice.paid", "invoice.payment_succeeded"):
        user = _user_for(db, obj)
        if user is not None and user.subscription is not None:
            lines = ((obj.get("lines") or {}).get("data") or [{}])[0]
            period = lines.get("period") or {}
            fallback = {
                "id": obj.get("subscription") or user.subscription.stripe_subscription_id,
                "status": "active",
                "current_period_start": period.get("start"),
                "current_period_end": period.get("end"),
                "metadata": {"plan": user.subscription.plan},
            }
            sub = _fetch_subscription(client, obj.get("subscription"), fallback)
            outcome = "renewed" if _apply_subscription(db, user, sub, created) else "stale"
    elif kind == "invoice.payment_failed":
        user = _user_for(db, obj)
        if user is not None and user.subscription is not None:
            sub = {
                "status": "past_due",
                "metadata": {"plan": user.subscription.plan},
                "id": user.subscription.stripe_subscription_id,
            }
            outcome = "past_due" if _apply_subscription(db, user, sub, created) else "stale"
    record.outcome = outcome
    if user is not None:
        record_event(db, "stripe", f"billing:{kind}", user, outcome=outcome, event=event_id)
    return outcome
