"""Start the interactive HighhX session — used by bare ``highhx`` and by ``highhx agent``.

One entry point, one UI. The account's capabilities decide whether the AI agent
session is attached (HighhX Pro) or requests are handled locally (HighhX Free); a
platform that is unreachable or refuses the agent leaves the local capabilities.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from highhx.actions import events as ev
from highhx.actions.events import EventLog
from highhx.cloud import capabilities
from highhx.cloud.capabilities import LOCAL, Capability, Connection
from highhx.core.errors import CloudError, HighhXError, UsageError

if TYPE_CHECKING:
    from highhx.agent.bootstrap import Resume
    from highhx.agent.session import AgentSession
    from highhx.cloud.account import Account
    from highhx.commands import App


def interactive_terminal(app: App) -> bool:
    """True when the interactive session may start: a terminal on both ends and no --json / --quiet."""
    options = app.options
    if options.json or options.quiet or not options.is_interactive():
        return False
    return bool(app.output.console.is_terminal)


def start_interactive(
    app: App,
    *,
    first: str | None = None,
    overrides: dict[str, Any] | None = None,
    resume: Resume | None = None,
    voice: bool = False,
) -> int:
    from highhx.agent.bootstrap import Resume, create_session
    from highhx.agent.repl import AgentREPL
    from highhx.agent.running import registered
    from highhx.agent.ui import TerminalUI

    if getattr(app, "interactive_session", False):
        raise UsageError("You are already in the HighhX session.", hint="Type your request at the prompt.")
    app.interactive_session = True  # type: ignore[attr-defined]
    out = app.output
    cloud = app.cloud
    ui = TerminalUI(out.console, out.symbols, interactive=True)
    entitlements = capabilities.resolve(cloud)

    def factory() -> tuple[AgentSession, Account, bool]:
        return create_session(app, cloud, ui, overrides=overrides or {}, resume=Resume())

    session: AgentSession | None = None
    account: Account | None = entitlements.account
    resumed = False
    if entitlements.has(Capability.AI_AGENT):
        try:
            session, account, resumed = create_session(app, cloud, ui, overrides=overrides or {}, resume=resume)
        except HighhXError as exc:
            # The platform decides: when it refuses or cannot be reached (or the agent cannot be
            # set up), the session starts with local capabilities instead of not at all.
            problem = "HighhX platform unavailable." if isinstance(exc, CloudError) else exc.message
            entitlements = dataclasses.replace(
                entitlements,
                capabilities=LOCAL,
                connection=Connection.UNAVAILABLE,
                problem=f"{problem} Local capabilities remain available.",
            )
    elif resume is not None and (resume.session_id or resume.latest):
        ui.notice("info", "Saved agent sessions are part of HighhX Pro — starting a local session.")
    from highhx.actions.events import events_dir

    first_run = not any(events_dir().glob("*.jsonl")) if events_dir().is_dir() else True
    repl = AgentREPL(
        session,
        ui,
        cloud,
        account,
        app=app,
        entitlements=entitlements,
        session_factory=factory,
        voice=voice,
        first_run=first_run,
    )
    session_id = session.record.id if session is not None and session.record else None
    log = EventLog(app.redactor, session_id=repl.session_id)
    detach = log.attach(app.ctx.events)
    if session is not None:
        app.ctx.events.emit(ev.AGENT_STARTED, session=repl.session_id, provider=session.provider.name)
    app.ctx.events.emit(
        ev.SESSION_STARTED,
        session=repl.session_id,
        tier=entitlements.tier.lower(),
        connection=str(entitlements.connection),
        agent=session is not None,
    )
    try:
        with registered(session_id, app.root):
            return repl.run(first, resumed=resumed)
    finally:
        app.ctx.events.emit(ev.SESSION_ENDED, session=repl.session_id)
        detach()
        if repl._actions is not None:
            repl._actions.close()
