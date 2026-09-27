"""Assemble an agent session from the CLI application, the account and settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from highhx.agent.context import gather
from highhx.agent.history import SessionRecord, SessionStore
from highhx.agent.memory import ProjectMemory
from highhx.agent.model.base import ModelProvider
from highhx.agent.model.platform import PlatformProvider
from highhx.agent.model.registry import PROVIDER_NAMES, provider_info
from highhx.agent.session import AgentSession, AgentUI
from highhx.agent.settings import AgentSettings
from highhx.agent.sync import SessionSync
from highhx.cloud.plans import AGENT, CLOUD_SESSIONS

if TYPE_CHECKING:
    from highhx.cloud.account import Account, CloudAccount
    from highhx.commands import App


@dataclass
class Resume:
    session_id: str | None = None
    latest: bool = False


def build_provider(settings: AgentSettings, cloud: CloudAccount, account: Account) -> ModelProvider:
    """The agent always talks to AI through the HighhX platform gateway.

    The platform authenticates the account, checks the Pro entitlement, applies the
    allowance and meters usage server-side; ``settings.provider`` only chooses which
    upstream (Anthropic, OpenAI, Gemini) the gateway routes to.
    """
    if settings.provider not in PROVIDER_NAMES:
        provider_info(settings.provider)  # raises a helpful error
    upstream = None if settings.provider == "highhx" else settings.provider
    return PlatformProvider(cloud.client(), upstream=upstream)


def resolve_settings(app: App, account: Account, overrides: dict[str, Any]) -> AgentSettings:
    project = app.config.raw.get("agent") if app.initialized else None
    account_settings = account.settings.get("agent") if isinstance(account.settings.get("agent"), dict) else {}
    settings = AgentSettings.resolve(
        project=project if isinstance(project, dict) else None,
        account=account_settings,
        overrides=overrides,
        plan_max_steps=account.plan.max_steps,
    )
    if settings.provider not in PROVIDER_NAMES:
        provider_info(settings.provider)  # raises a helpful error
    return settings


def create_session(
    app: App,
    cloud: CloudAccount,
    ui: AgentUI,
    *,
    overrides: dict[str, Any] | None = None,
    resume: Resume | None = None,
) -> tuple[AgentSession, Account, bool]:
    """Returns ``(session, account, resumed)``. Raises unless the account includes the agent."""
    account = cloud.require(AGENT, what="The HighhX AI developer agent")
    settings = resolve_settings(app, account, overrides or {})
    provider = build_provider(settings, cloud, account)
    context = gather(app)
    memory = ProjectMemory.for_project(app.root, initialized=app.initialized, redactor=app.redactor)
    store = SessionStore(app.db, app.redactor) if app.db is not None else None
    record: SessionRecord | None = None
    resumed = False
    if store is not None and resume is not None and (resume.session_id or resume.latest):
        record = (
            store.get(resume.session_id, account_id=account.id)
            if resume.session_id
            else store.latest(str(app.root), account_id=account.id)
        )
        if record is None:
            ui.notice("info", "No earlier session for this project — starting a new one.")
        else:
            resumed = True
    if store is not None and record is None:
        record = store.create(
            title="New session", root=str(app.root), provider=provider.name, model=settings.model, account_id=account.id
        )
    sync = SessionSync(cloud) if settings.sync_sessions and account.has(CLOUD_SESSIONS) else None
    session = AgentSession(
        app,
        provider,
        settings,
        ui,
        features=account.features,
        context=context,
        memory=memory,
        store=store,
        record=record,
        sync=sync,
        account_id=account.id,
    )
    if resumed and store is not None and record is not None:
        session.load_transcript(store.messages(record.id))
        session.plan = record.plan
        session.usage = record.usage
    return session, account, resumed
