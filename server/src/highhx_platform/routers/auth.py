"""Sign-up, sign-in, the device flow used by `highhx login`, and API tokens."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from highhx_platform import accounts, security
from highhx_platform.accounts import AccountProblem
from highhx_platform.config import Settings
from highhx_platform.deps import Principal, current_principal, get_db, get_settings, problem
from highhx_platform.models import ApiToken, DeviceAuthorization, utcnow

router = APIRouter(prefix="/v1", tags=["auth"])


def limits(request: Request) -> security.Limits:
    found: security.Limits = request.app.state.limits
    return found


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Credentials(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=500)
    name: str = Field(default="", max_length=200)
    token_name: str = Field(default="api", max_length=200)


@router.post("/auth/signup", status_code=201)
def signup(
    body: Credentials, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    if not settings.signup_enabled:
        raise problem(403, "signup_disabled", "Sign-ups are closed on this HighhX platform.")
    if not limits(request).signup.allow(client_ip(request)):
        raise problem(429, "rate_limited", "Too many sign-ups from this address. Try again later.")
    try:
        user = accounts.create_user(db, body.email, body.password, body.name)
    except AccountProblem as exc:
        raise problem(exc.status, exc.code, exc.message, exc.hint) from None
    token = accounts.issue_token(db, user, body.token_name, ttl_days=settings.token_ttl_days)
    accounts.record_event(db, f"user:{user.id}", "signup", user, ip=client_ip(request))
    return {"access_token": token, "account": accounts.account_document(db, user)}


@router.post("/auth/login")
def login(
    body: Credentials, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    key = f"{client_ip(request)}|{body.email.strip().lower()}"
    if not limits(request).login.allow(key):
        raise problem(429, "rate_limited", "Too many sign-in attempts. Try again in 15 minutes.")
    try:
        user = accounts.authenticate(db, body.email, body.password)
    except AccountProblem as exc:
        raise problem(exc.status, exc.code, exc.message) from None
    limits(request).login.reset(key)
    if user.suspended_at is not None:
        raise problem(403, "account_suspended", "This HighhX account is suspended.", "Contact HighhX support.")
    token = accounts.issue_token(db, user, body.token_name, ttl_days=settings.token_ttl_days)
    accounts.record_event(db, f"user:{user.id}", "login", user, ip=client_ip(request))
    return {"access_token": token, "account": accounts.account_document(db, user)}


@router.post("/auth/refresh")
def refresh(
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Rotate the current token: a new token is issued and the old one is revoked."""
    old = db.get(ApiToken, principal.token.id)
    if old is None or old.revoked_at is not None:
        raise problem(401, "unauthorized", "Your HighhX session expired or was revoked.", "Run `highhx login`.")
    old.revoked_at = utcnow()
    token = accounts.issue_token(db, principal.user, old.name, ttl_days=settings.token_ttl_days)
    accounts.record_event(db, f"user:{principal.user.id}", "token_refresh", principal.user)
    return {"access_token": token}


@router.post("/auth/logout")
def logout(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    token = db.get(ApiToken, principal.token.id)
    if token is not None and token.revoked_at is None:
        token.revoked_at = utcnow()
        accounts.record_event(db, f"user:{principal.user.id}", "logout", principal.user)
    return {"revoked": True}


# ------------------------------------------------------------------- device flow
class DeviceStart(BaseModel):
    client: str = Field(default="", max_length=100)
    version: str = Field(default="", max_length=32)
    hostname: str = Field(default="", max_length=200)


@router.post("/auth/device")
def device_start(
    body: DeviceStart, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    if not limits(request).device.allow(client_ip(request)):
        raise problem(429, "rate_limited", "Too many sign-in attempts. Try again later.")
    device_code = security.new_device_code()
    user_code = security.new_user_code()
    db.add(
        DeviceAuthorization(
            device_code_hash=security.hash_token(device_code),
            user_code=user_code,
            client=f"{body.client} {body.version}".strip(),
            hostname=body.hostname,
            expires_at=utcnow() + timedelta(seconds=settings.device_code_ttl),
        )
    )
    uri = f"{settings.public_url}/device"
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": uri,
        "verification_uri_complete": f"{uri}?code={user_code}",
        "expires_in": settings.device_code_ttl,
        "interval": settings.device_poll_interval,
    }


class DevicePoll(BaseModel):
    device_code: str = Field(max_length=200)


@router.post("/auth/device/token")
def device_token(
    body: DevicePoll, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    auth = db.scalar(
        select(DeviceAuthorization).where(DeviceAuthorization.device_code_hash == security.hash_token(body.device_code))
    )
    if auth is None:
        raise problem(400, "invalid_device_code", "Unknown sign-in code.")
    now = utcnow()
    if _aware(auth.expires_at) < now and auth.status in ("pending", "approved"):
        auth.status = "expired"
    if auth.status == "pending":
        last = auth.last_polled_at
        auth.last_polled_at = now
        if last is not None and (now - _aware(last)).total_seconds() < settings.device_poll_interval - 1:
            return {"status": "slow_down"}
        return {"status": "pending"}
    if auth.status == "approved" and auth.user_id:
        auth.status = "consumed"
        user = db.get(accounts.User, auth.user_id)
        if user is None:
            raise problem(400, "invalid_device_code", "Unknown sign-in code.")
        name = f"HighhX CLI on {auth.hostname or 'unknown host'}"
        token = accounts.issue_token(db, user, name, ttl_days=settings.token_ttl_days)
        accounts.record_event(db, f"user:{user.id}", "device_login", user, host=auth.hostname)
        return {"status": "approved", "access_token": token}
    return {"status": "expired" if auth.status in ("expired", "consumed") else auth.status}


def approve_device(db: Session, user_code: str, user: accounts.User) -> DeviceAuthorization:
    auth = db.scalar(
        select(DeviceAuthorization).where(
            DeviceAuthorization.user_code == security.normalize_user_code(user_code),
            DeviceAuthorization.status == "pending",
        )
    )
    if auth is None or _aware(auth.expires_at) < utcnow():
        raise AccountProblem(400, "invalid_user_code", "That code is invalid or has expired. Run `highhx login` again.")
    auth.status = "approved"
    auth.user_id = user.id
    return auth


# ------------------------------------------------------------------------ tokens
class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@router.get("/tokens")
def list_tokens(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    tokens = db.scalars(
        select(ApiToken)
        .where(ApiToken.user_id == principal.user.id, ApiToken.revoked_at.is_(None))
        .order_by(ApiToken.created_at)
    ).all()
    return {
        "items": [
            {
                "id": t.id,
                "name": t.name,
                "prefix": t.prefix,
                "created_at": t.created_at.isoformat(),
                "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
                "current": t.id == principal.token.id,
            }
            for t in tokens
        ]
    }


@router.post("/tokens", status_code=201)
def create_token(
    body: TokenCreate,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    token = accounts.issue_token(db, principal.user, body.name, ttl_days=settings.token_ttl_days)
    return {"access_token": token, "name": body.name, "note": "Store this token now; it is not shown again."}


@router.delete("/tokens/{token_id}")
def revoke_token(
    token_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)
) -> dict[str, Any]:
    token = db.get(ApiToken, token_id)
    if token is None or token.user_id != principal.user.id:
        raise problem(404, "not_found", "No such token.")
    token.revoked_at = token.revoked_at or utcnow()
    return {"revoked": True}
