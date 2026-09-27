"""FastAPI dependencies: database sessions, settings and the authenticated user."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from highhx.cloud.plans import Plan
from highhx_platform.accounts import effective_plan, user_for_token
from highhx_platform.config import Settings
from highhx_platform.models import ApiToken, User


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.db.sessions() as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise


def problem(status: int, code: str, message: str, hint: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "hint": hint})


@dataclass
class Principal:
    user: User
    token: ApiToken

    @property
    def plan(self) -> Plan:
        return effective_plan(self.user)


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise problem(401, "unauthorized", "Sign in required.", "Run `highhx login`.")
    found = user_for_token(db, token.strip())
    if found is None:
        raise problem(401, "unauthorized", "Your HighhX session expired or was revoked.", "Run `highhx login`.")
    if found[0].suspended_at is not None:
        raise problem(403, "account_suspended", "This HighhX account is suspended.", "Contact HighhX support.")
    db.commit()  # persist last_used_at
    return Principal(*found)


def require_feature(feature: str, what: str) -> Any:
    """A dependency marker yielding the :class:`Principal` when their plan includes ``feature``."""

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if feature not in principal.plan.features:
            raise problem(
                402,
                "plan_required",
                f"{what} requires HighhX Pro (you are on {principal.plan.name}).",
                "Upgrade with `highhx account upgrade`.",
            )
        return principal

    return Depends(dependency)
