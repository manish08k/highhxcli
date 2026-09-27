"""The signed-in account: profile, plan, usage, settings, projects and agent sessions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field
from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from highhx.agent.state import LEGACY, SessionState, can_transition, parse_state, sources_for
from highhx.cloud.plans import AGENT, CLOUD_SESSIONS
from highhx_platform import accounts
from highhx_platform.accounts import AccountProblem
from highhx_platform.deps import Principal, current_principal, get_db, problem, require_feature
from highhx_platform.models import AgentSession, Project, User, utcnow

router = APIRouter(prefix="/v1", tags=["account"])


@router.get("/me")
def me(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    return accounts.account_document(db, principal.user)


@router.patch("/me/settings")
def update_settings(
    body: dict[str, Any] = Body(...), principal: Principal = Depends(current_principal), db: Session = Depends(get_db)
) -> dict[str, Any]:
    unknown = set(body) - {"agent"}
    if unknown:
        raise problem(400, "invalid_setting", f"unknown settings: {', '.join(sorted(unknown))}")
    agent = body.get("agent") or {}
    if not isinstance(agent, dict):
        raise problem(400, "invalid_setting", "agent must be an object")
    user = db.get(User, principal.user.id)
    if user is None:
        raise problem(404, "not_found", "Account not found.")
    try:
        settings = accounts.update_agent_settings(user, agent)
    except AccountProblem as exc:
        raise problem(exc.status, exc.code, exc.message) from None
    return settings


@router.get("/usage")
def usage(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    return accounts.usage_summary(db, principal.user).to_dict()


# ---------------------------------------------------------------------- projects
class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    fingerprint: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    stack: str = Field(default="", max_length=200)


def _project(p: Project) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "stack": p.stack,
        "created_at": p.created_at.isoformat(),
        "last_seen_at": p.last_seen_at.isoformat(),
    }


@router.post("/projects")
def register_project(
    body: ProjectIn, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)
) -> dict[str, Any]:
    project = db.scalar(
        select(Project).where(Project.user_id == principal.user.id, Project.fingerprint == body.fingerprint)
    )
    if project is None:
        project = Project(user_id=principal.user.id, name=body.name, fingerprint=body.fingerprint, stack=body.stack)
        db.add(project)
    else:
        project.name, project.stack, project.last_seen_at = body.name, body.stack, utcnow()
    db.flush()
    return _project(project)


@router.get("/projects")
def list_projects(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(
        select(Project).where(Project.user_id == principal.user.id).order_by(Project.last_seen_at.desc()).limit(200)
    ).all()
    return {"items": [_project(p) for p in rows]}


# ---------------------------------------------------------------- agent sessions
class SessionIn(BaseModel):
    project_id: str | None = Field(default=None, max_length=32)
    title: str = Field(default="", max_length=300)
    provider: str = Field(default="", max_length=32)
    model: str | None = Field(default=None, max_length=100)
    client_version: str = Field(default="", max_length=32)
    local_id: str = Field(default="", max_length=64)


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    status: str | None = Field(default=None, max_length=32)
    turns: int | None = Field(default=None, ge=0)
    provider: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=100)
    usage: dict[str, int] | None = None


def _session(s: AgentSession) -> dict[str, Any]:
    return {
        "id": s.id,
        "project_id": s.project_id,
        "title": s.title,
        "status": s.status,
        "provider": s.provider,
        "model": s.model,
        "turns": s.turns,
        "usage": s.usage or {},
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


sessions_feature = require_feature(CLOUD_SESSIONS, "Session sync")


@router.post("/agent/sessions", status_code=201)
def create_session(
    body: SessionIn, principal: Principal = sessions_feature, db: Session = Depends(get_db)
) -> dict[str, Any]:
    project_id = body.project_id
    if project_id:
        project = db.get(Project, project_id)
        if project is None or project.user_id != principal.user.id:
            project_id = None
    session = AgentSession(
        user_id=principal.user.id,
        project_id=project_id,
        title=body.title,
        provider=body.provider,
        model=body.model,
        client_version=body.client_version,
        local_id=body.local_id,
        usage={},
        status=str(SessionState.CREATED),
    )
    db.add(session)
    db.flush()
    return _session(session)


@router.patch("/agent/sessions/{session_id}")
def update_session(
    session_id: str, body: SessionPatch, principal: Principal = sessions_feature, db: Session = Depends(get_db)
) -> dict[str, Any]:
    session = db.get(AgentSession, session_id)
    if session is None or session.user_id != principal.user.id:
        raise problem(404, "not_found", "No such session.")
    changes = body.model_dump(exclude_none=True)
    target = changes.pop("status", None)
    if target is not None:
        try:
            new_state = parse_state(target)
            current = parse_state(session.status)
        except ValueError:
            raise problem(400, "invalid_status", f"Unknown session status {target!r}.") from None
        if not can_transition(current, new_state):
            raise problem(409, "invalid_transition", f"A session cannot go from {current} to {new_state}.")
        # Atomic compare-and-set: a concurrent update that already moved the session wins.
        moved: CursorResult[Any] = db.execute(  # type: ignore[assignment]
            update(AgentSession)
            .where(
                AgentSession.id == session_id,
                AgentSession.user_id == principal.user.id,
                AgentSession.status.in_(
                    [str(s) for s in sources_for(new_state)]
                    + [k for k, v in LEGACY.items() if v in sources_for(new_state)]
                ),
            )
            .values(status=str(new_state), updated_at=utcnow())
        )
        if moved.rowcount != 1:
            raise problem(409, "invalid_transition", "The session changed concurrently; refresh and retry.")
        db.refresh(session)
    for key, value in changes.items():
        setattr(session, key, value)
    db.flush()
    return _session(session)


@router.get("/agent/sessions")
def list_sessions(
    principal: Principal = require_feature(AGENT, "Agent sessions"), db: Session = Depends(get_db)
) -> dict[str, Any]:
    rows = db.scalars(
        select(AgentSession)
        .where(AgentSession.user_id == principal.user.id)
        .order_by(AgentSession.updated_at.desc())
        .limit(100)
    ).all()
    return {"items": [_session(s) for s in rows]}
