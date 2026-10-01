"""Where HighhX's connections stand — read from the live objects, never remembered.

    model        the agent's provider: ready, or failing (its circuit breaker is open), and whether
                 it takes images
    computer     the automation engine answers ``status`` — on this computer or a remote one
    browser      the HighhX browser's own state machine (connected, page valid, recovering …)
    mcp:<name>   each external MCP server, pinged now
    attachments  the files attached to the conversation

Each row is one of: connected · ready · not_started · unavailable · failing · error. A service is
shown ``connected`` only when it just answered; a part that was never started says so, and is not
started by looking at it (the computer engine excepted: asking it is how its state is known, and
it is cheap and local — or, for a remote target, the only way to know the link is up).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from highhx.agent.session import AgentSession
    from highhx.commands import App

CONNECTED, READY, NOT_STARTED, UNAVAILABLE, FAILING, ERROR = (
    "connected",
    "ready",
    "not_started",
    "unavailable",
    "failing",
    "error",
)


@dataclass(frozen=True)
class ConnectionState:
    name: str
    state: str
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return self.state in (CONNECTED, READY)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "detail": self.detail}


def model_state(session: AgentSession | None) -> ConnectionState:
    if session is None:
        return ConnectionState("model", NOT_STARTED, "HighhX Free: requests run without AI")
    caps = session.capabilities
    what = f"{session.provider.name} · {caps.model}" + (" · sees images" if caps.vision else " · text only")
    if session.breaker.open:
        return ConnectionState("model", FAILING, f"{what} — recent requests failed; retrying shortly")
    return ConnectionState("model", READY, what)


def computer_state(app: App, session: AgentSession | None = None) -> ConnectionState:
    from highhx.automation.engine.bridge import engine_status
    from highhx.computer.desktop import run_cancellable

    target = computer_target(app)
    if target != "local":
        driver = session._computer.driver() if session is not None and session._computer is not None else None
        if driver is None:
            return ConnectionState("computer", NOT_STARTED, f"{target} (connects on first use)")
        try:
            status = driver.status()
        except Exception as exc:
            return ConnectionState("computer", ERROR, f"{target}: {getattr(exc, 'message', exc)}")
        return ConnectionState("computer", CONNECTED if status.get("ok", True) else UNAVAILABLE, f"{target} · {driver.name}")

    def runner(argv: list[str], what: str) -> tuple[int, str, str]:
        return run_cancellable(argv, timeout=20, cancel=None, what=what)

    status = engine_status(runner)
    if status.get("engine") is None:
        return ConnectionState("computer", ERROR, str(status.get("detail") or "no automation engine"))
    detail = f"this computer · {status['engine']} engine"
    if not status.get("ok", True):
        return ConnectionState("computer", UNAVAILABLE, f"{detail} — {status.get('detail') or 'not ready'}")
    if status.get("accessibility") is False:
        return ConnectionState("computer", UNAVAILABLE, f"{detail} — Accessibility is not granted")
    return ConnectionState("computer", CONNECTED, detail)


def browser_state(session: AgentSession | None) -> ConnectionState:
    computer = session._computer if session is not None else None
    browser = computer._browser if computer is not None else None
    if browser is None:
        return ConnectionState("browser", NOT_STARTED, "starts on first use")
    state = str(browser.state)
    endpoint = getattr(browser, "shown_endpoint", "") or ""
    where = f" · {endpoint}" if endpoint else ""
    if state in ("connected", "page_valid", "navigation_in_progress"):
        return ConnectionState("browser", CONNECTED, f"{state}{where} · {browser.reconnects} recovery(ies)")
    if state == "stopped":
        return ConnectionState("browser", NOT_STARTED, "stopped")
    return ConnectionState("browser", FAILING, f"{state}{where} — HighhX recovers it on the next action")


def mcp_states(session: AgentSession | None) -> list[ConnectionState]:
    manager = session.mcp if session is not None else None
    if manager is None:
        return []
    out = []
    for row in manager.status():
        state = {"connected": CONNECTED, "disabled": NOT_STARTED, "disconnected": FAILING}.get(row["state"], ERROR)
        out.append(ConnectionState(f"mcp:{row['server']}", state, f"{row['tools']} tool(s) · {row['detail']}"))
    return out


def computer_target(app: App) -> str:
    """``local``, or the remote computer this project/environment points at (``ssh://…``)."""
    import os

    configured = os.environ.get("HIGHHX_COMPUTER_TARGET", "")
    if not configured and app.initialized:
        section = app.config.raw.get("computer")
        if isinstance(section, dict) and section.get("target"):
            configured = str(section["target"])
    return configured or "local"


def browser_endpoint(app: App) -> str:
    """An existing browser's DevTools endpoint to use instead of HighhX's own ('' for none)."""
    import os

    configured = os.environ.get("HIGHHX_BROWSER_ENDPOINT", "")
    if not configured and app.initialized:
        section = app.config.raw.get("computer")
        if isinstance(section, dict) and section.get("browser_endpoint"):
            configured = str(section["browser_endpoint"])
    return configured


def all_states(app: App, session: AgentSession | None) -> list[ConnectionState]:
    rows = [model_state(session), computer_state(app, session), browser_state(session), *mcp_states(session)]
    if session is not None and session.attachments.items:
        names = ", ".join(f"{a.id} {a.name}" for a in session.attachments.items.values())
        rows.append(ConnectionState("attachments", READY, names))
    return rows
