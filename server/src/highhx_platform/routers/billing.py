"""Billing endpoints (Stripe) and the support admin endpoint."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from highhx.cloud.plans import PLANS, PRO
from highhx_platform import accounts
from highhx_platform.billing import (
    BillingError,
    StripeClient,
    create_checkout,
    create_portal,
    handle_event,
    verify_signature,
)
from highhx_platform.config import Settings
from highhx_platform.deps import Principal, current_principal, get_db, get_settings, problem
from highhx_platform.models import User, utcnow
from highhx_platform.security import constant_time_equals

router = APIRouter(prefix="/v1", tags=["billing"])


def stripe_client(request: Request, settings: Settings) -> StripeClient:
    factory = getattr(request.app.state, "stripe_factory", None)
    if factory is not None:
        client: StripeClient = factory(settings)
        return client
    if not settings.stripe_secret_key:
        raise problem(503, "billing_unavailable", "Billing is not configured on this HighhX platform.")
    return StripeClient(settings.stripe_secret_key)


class CheckoutIn(BaseModel):
    plan: str = Field(default=PRO)


@router.post("/billing/checkout")
def checkout(
    body: CheckoutIn,
    request: Request,
    principal: Principal = Depends(current_principal),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if body.plan != PRO:
        raise problem(400, "invalid_plan", "Only HighhX Pro can be purchased.")
    if principal.plan.id == PRO:
        raise problem(409, "already_pro", "You already have HighhX Pro.", "Manage it with `highhx account billing`.")
    if not settings.billing_enabled:
        raise problem(503, "billing_unavailable", "Billing is not configured on this HighhX platform.")
    try:
        url = create_checkout(settings, stripe_client(request, settings), principal.user)
    except BillingError as exc:
        raise problem(exc.status, "billing_error", exc.message) from None
    return {"url": url}


@router.post("/billing/portal")
def portal(
    request: Request, principal: Principal = Depends(current_principal), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    try:
        url = create_portal(settings, stripe_client(request, settings), principal.user)
    except BillingError as exc:
        raise problem(exc.status, "billing_error", exc.message) from None
    return {"url": url}


@router.post("/billing/webhook", include_in_schema=False)
async def webhook(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    if not settings.stripe_webhook_secret:
        raise problem(503, "billing_unavailable", "Webhooks are not configured.")
    payload = await request.body()
    try:
        verify_signature(payload, request.headers.get("stripe-signature", ""), settings.stripe_webhook_secret)
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise BillingError(400, "Invalid event payload.")
        client = stripe_client(request, settings) if settings.stripe_secret_key else None
        outcome = handle_event(db, event, client)
        db.commit()
    except IntegrityError:
        # A concurrent delivery of the same event committed first; nothing from this one applies.
        db.rollback()
        return {"received": True, "outcome": "duplicate"}
    except BillingError as exc:
        raise problem(exc.status, "webhook_rejected", exc.message) from None
    except ValueError:
        raise problem(400, "webhook_rejected", "Invalid JSON.") from None
    return {"received": True, "outcome": outcome}


class PlanChange(BaseModel):
    email: str = Field(max_length=320)
    plan: str


@router.post("/admin/plan", include_in_schema=False)
def admin_set_plan(
    body: PlanChange, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Support tool: grant or remove a plan manually (comped accounts, refunds)."""
    if not constant_time_equals(request.headers.get("x-admin-token"), settings.admin_token):
        raise problem(403, "forbidden", "Admin token required.")
    if body.plan not in PLANS:
        raise problem(400, "invalid_plan", f"plan must be one of: {', '.join(PLANS)}")
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is None:
        raise problem(404, "not_found", "No such user.")
    previous = user.plan
    user.plan = body.plan
    accounts.record_event(db, "admin", "plan_change", user, previous=previous, plan=body.plan)
    return accounts.account_document(db, user)


class Suspension(BaseModel):
    email: str = Field(max_length=320)
    suspended: bool = True
    reason: str = Field(default="", max_length=500)


@router.post("/admin/suspend", include_in_schema=False)
def admin_suspend(
    body: Suspension, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Support tool: suspend or reinstate an account. Suspended accounts cannot use any token."""
    if not constant_time_equals(request.headers.get("x-admin-token"), settings.admin_token):
        raise problem(403, "forbidden", "Admin token required.")
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is None:
        raise problem(404, "not_found", "No such user.")
    user.suspended_at = utcnow() if body.suspended else None
    accounts.record_event(db, "admin", "suspend" if body.suspended else "reinstate", user, reason=body.reason)
    return {"email": user.email, "suspended": body.suspended}
