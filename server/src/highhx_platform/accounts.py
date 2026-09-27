"""Accounts, tokens, entitlements and usage — the platform's business rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from highhx.cloud.plans import FREE, PRO, Plan, plan_for
from highhx_platform import security
from highhx_platform.models import AgentSession, ApiToken, AuditEvent, UsageRecord, User, utcnow

ACTIVE_STATUSES = ("active", "trialing", "past_due")
"""Stripe subscription states that keep Pro (past_due while Stripe retries the payment)."""
AGENT_SETTING_KEYS = {
    "provider": ("highhx", "anthropic", "openai", "gemini"),
    "upstream": ("anthropic", "openai", "gemini"),
    "approval": ("ask", "auto-edit", "read-only"),
}


class AccountProblem(Exception):
    def __init__(self, status: int, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "hint": self.hint}


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or len(email) > 320 or email.startswith("@") or email.endswith("@"):
        raise AccountProblem(400, "invalid_email", "Enter a valid email address.")
    return email


def create_user(db: Session, email: str, password: str, name: str = "") -> User:
    email = normalize_email(email)
    if len(password) < 10:
        raise AccountProblem(400, "weak_password", "Passwords need at least 10 characters.")
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise AccountProblem(409, "email_taken", "An account with this email already exists.", "Sign in instead.")
    user = User(email=email, name=name.strip()[:200], password_hash=security.hash_password(password), settings={})
    db.add(user)
    db.flush()
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    try:
        email = normalize_email(email)
    except AccountProblem:
        raise AccountProblem(401, "invalid_credentials", "Wrong email or password.") from None
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or not security.verify_password(password, user.password_hash):
        raise AccountProblem(401, "invalid_credentials", "Wrong email or password.")
    return user


def issue_token(db: Session, user: User, name: str, *, ttl_days: int = 90) -> str:
    token = security.new_token()
    db.add(
        ApiToken(
            user_id=user.id,
            name=name[:200],
            token_hash=security.hash_token(token),
            prefix=token[:10],
            expires_at=utcnow() + timedelta(days=ttl_days) if ttl_days > 0 else None,
        )
    )
    db.flush()
    return token


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def record_event(db: Session, actor: str, action: str, subject: User | None = None, **details: Any) -> None:
    db.add(AuditEvent(actor=actor, action=action, subject_user_id=subject.id if subject else None, details=details))


def user_for_token(db: Session, token: str) -> tuple[User, ApiToken] | None:
    """The token's user, or None if it is unknown, revoked, expired or its user is deactivated.
    Suspension is checked separately so the caller can explain it."""
    record = db.scalar(select(ApiToken).where(ApiToken.token_hash == security.hash_token(token)))
    if record is None or record.revoked_at is not None or not record.user.is_active:
        return None
    if record.expires_at is not None and _aware(record.expires_at) <= utcnow():
        return None
    now = utcnow()
    last = record.last_used_at
    if last is None or (now - (last if last.tzinfo else last.replace(tzinfo=UTC))).total_seconds() > 60:
        record.last_used_at = now
    return record.user, record


# ------------------------------------------------------------------ entitlements
SUBSCRIPTION_GRACE = timedelta(days=3)


def effective_plan(user: User, *, now: datetime | None = None) -> Plan:
    """The plan the account is entitled to *now*, from verified billing state only.

    A subscription counts while Stripe reports it active/trialing/past_due and its paid
    period (plus a short grace for delayed renewal webhooks) has not ended.
    """
    now = now or utcnow()
    subscription = user.subscription
    if subscription is not None and subscription.status in ACTIVE_STATUSES:
        end = subscription.current_period_end
        if end is None or _aware(end) + SUBSCRIPTION_GRACE > now:
            return plan_for(subscription.plan)
        return plan_for(FREE)
    if subscription is not None and subscription.status not in ACTIVE_STATUSES and user.plan == PRO:
        # A lapsed paid subscription does not keep a Pro plan that came from billing.
        return plan_for(FREE)
    return plan_for(user.plan)


def billing_period(user: User) -> tuple[datetime, datetime]:
    subscription = user.subscription
    if subscription is not None and subscription.current_period_start and subscription.current_period_end:
        start, end = subscription.current_period_start, subscription.current_period_end
        return (start if start.tzinfo else start.replace(tzinfo=UTC)), (end if end.tzinfo else end.replace(tzinfo=UTC))
    now = utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


@dataclass
class UsageSummary:
    period_start: datetime
    period_end: datetime
    tokens_used: int
    tokens_included: int
    requests: int
    sessions: int
    by_model: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "tokens_used": self.tokens_used,
            "tokens_included": self.tokens_included,
            "requests": self.requests,
            "sessions": self.sessions,
            "by_model": self.by_model,
        }


def usage_summary(db: Session, user: User) -> UsageSummary:
    start, end = billing_period(user)
    rows = db.execute(
        select(
            UsageRecord.provider,
            UsageRecord.model,
            func.count(UsageRecord.id),
            func.coalesce(func.sum(UsageRecord.input_tokens), 0),
            func.coalesce(func.sum(UsageRecord.output_tokens), 0),
        )
        .where(UsageRecord.user_id == user.id, UsageRecord.created_at >= start, UsageRecord.created_at < end)
        .group_by(UsageRecord.provider, UsageRecord.model)
    ).all()
    by_model: list[dict[str, Any]] = []
    tokens = requests = 0
    for provider, model, count, input_tokens, output_tokens in rows:
        by_model.append(
            {
                "provider": provider,
                "model": model,
                "requests": int(count),
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
            }
        )
        tokens += int(input_tokens) + int(output_tokens)
        requests += int(count)
    sessions = db.scalar(
        select(func.count(AgentSession.id)).where(AgentSession.user_id == user.id, AgentSession.created_at >= start)
    )
    return UsageSummary(
        start,
        end,
        tokens,
        effective_plan(user).monthly_tokens,
        requests,
        int(sessions or 0),
        by_model,
    )


def account_document(db: Session, user: User) -> dict[str, Any]:
    """The `/v1/me` document the CLI caches."""
    plan = effective_plan(user)
    subscription = user.subscription
    return {
        "user": {"id": user.id, "email": user.email, "name": user.name},
        "plan": plan.id,
        "features": sorted(plan.features),
        "limits": {"monthly_tokens": plan.monthly_tokens, "max_steps": plan.max_steps},
        "usage": usage_summary(db, user).to_dict(),
        "settings": user.settings or {},
        "subscription": {
            "status": subscription.status,
            "plan": subscription.plan,
            "current_period_end": subscription.current_period_end.isoformat()
            if subscription.current_period_end
            else None,
            "cancel_at_period_end": subscription.cancel_at_period_end,
        }
        if subscription
        else {},
    }


def update_agent_settings(user: User, changes: dict[str, Any]) -> dict[str, Any]:
    agent = dict((user.settings or {}).get("agent") or {})
    for key, value in changes.items():
        if key in AGENT_SETTING_KEYS:
            if value is not None and value not in AGENT_SETTING_KEYS[key]:
                raise AccountProblem(
                    400, "invalid_setting", f"{key} must be one of: {', '.join(AGENT_SETTING_KEYS[key])}"
                )
        elif key == "model":
            if value is not None and (not isinstance(value, str) or not 0 < len(value) <= 100):
                raise AccountProblem(400, "invalid_setting", "model must be a non-empty string")
        elif key == "sync_sessions":
            if not isinstance(value, bool):
                raise AccountProblem(400, "invalid_setting", "sync_sessions must be true or false")
        else:
            raise AccountProblem(400, "invalid_setting", f"unknown agent setting '{key}'")
        if value is None:
            agent.pop(key, None)
        else:
            agent[key] = value
    user.settings = {**(user.settings or {}), "agent": agent}
    return user.settings
