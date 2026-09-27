"""Browser connection failures never cause an action to run twice.

A command whose answer is lost (connection closed, browser crashed, timeout) has an
*unknown* outcome: it is reported and audited as such, the transport never re-sends it,
and the runtime refuses to repeat it — also after the browser reconnects.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from highhx.commands import App
from highhx.computer.browser import CDPConnection
from highhx.computer.runtime import ComputerRuntime
from highhx.core.context import Options
from highhx.core.errors import IntegrationError, NotFoundError, OutcomeUnknownError
from highhx.execution.cancellation import CancellationToken
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.unit.agent.conftest import RecordingUI
from tests.unit.computer.conftest import FakeShop
from tests.unit.computer.test_computer import FakeDevTools


@pytest.fixture
def app(tmp_path: Path) -> Iterator[App]:
    application = App(Options(interactive=True), cwd=tmp_path)
    yield application
    application.close()


def _runtime(app: App, shop: FakeShop, ui: RecordingUI) -> ComputerRuntime:
    gate = ActionGate(
        app.engine, ui, source="computer", mode=ApprovalMode.AUTO_EDIT, audit=AuditLog(app.db, app.redactor)
    )
    return ComputerRuntime(shop, gate, actor=Actor.AGENT, settle=0)


def _audit_statuses(app: App) -> list[str]:
    return [str(row["status"]) for row in app.db.query("SELECT status FROM audit_log ORDER BY id")]


# ------------------------------------------------------------------ transport
@pytest.mark.parametrize("drop", ["close", "reset"])
def test_lost_answer_is_an_unknown_outcome_and_is_never_resent(drop: str) -> None:
    server = FakeDevTools()
    server.drop = drop
    conn = CDPConnection(f"ws://127.0.0.1:{server.port}/devtools/page/1")
    with pytest.raises(OutcomeUnknownError, match="may or may not have run"):
        conn.call("Runtime.evaluate", {"expression": "document.querySelector('#delete').click()"})
    assert len(server.received) == 1  # sent exactly once
    assert conn.ws.closed
    server.close()


def test_timeout_is_an_unknown_outcome_and_closes_the_connection() -> None:
    """A late answer must not be mistaken for the answer to a later command."""
    server = FakeDevTools()
    server.stall = True
    conn = CDPConnection(f"ws://127.0.0.1:{server.port}/devtools/page/1", timeout=0.5)
    with pytest.raises(OutcomeUnknownError, match="did not answer"):
        conn.call("Runtime.evaluate", {"expression": "1"}, cancel=CancellationToken())
    assert conn.ws.closed and len(server.received) == 1
    server.close()


# ------------------------------------------------------------------ runtime
class DroppingShop(FakeShop):
    """The browser performs the click, then the connection drops before the answer arrives."""

    def __init__(self) -> None:
        super().__init__()
        self.drop_next_click = False

    def click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        super().click(element_id, cancel=cancel)
        if self.drop_next_click:
            self.drop_next_click = False
            raise OutcomeUnknownError("The browser connection was lost after Runtime.evaluate was sent.")


def test_destructive_action_with_lost_answer_is_never_repeated_after_reconnect(app: App) -> None:
    shop = DroppingShop()
    ui = RecordingUI()
    rt = _runtime(app, shop, ui)
    rt.observe()
    shop.drop_next_click = True
    with pytest.raises(OutcomeUnknownError):
        rt.act("click:e6")  # "Delete account" — confirmed by the user, performed, answer lost
    assert shop.deleted and shop.actions.count("click Delete account") == 1
    assert _audit_statuses(app)[-1] == "unknown"
    assert rt.observation is None  # the next decision starts from a fresh observation
    confirmations = len(ui.requests)

    # The browser reconnects; the page still shows the button (e.g. the deletion is async).
    shop.deleted = False
    rt.observe()
    with pytest.raises(IntegrationError, match="Not retrying automatically"):
        rt.act("click:e6")
    assert shop.actions.count("click Delete account") == 1  # still exactly once
    assert len(ui.requests) == confirmations  # not even offered for confirmation again


def test_non_sensitive_action_with_lost_answer_is_not_repeated_either(app: App) -> None:
    shop = DroppingShop()
    rt = _runtime(app, shop, RecordingUI())
    rt.observe()
    shop.drop_next_click = True
    with pytest.raises(OutcomeUnknownError):
        rt.act("click:e5")  # Newsletter checkbox
    rt.observe()
    with pytest.raises(IntegrationError, match="outcome is unknown"):
        rt.act("click:e5")
    assert shop.actions.count("click Newsletter") == 1


def test_completed_action_is_not_replayed_when_the_connection_drops_afterwards(app: App) -> None:
    shop = FakeShop()
    rt = _runtime(app, shop, RecordingUI())
    rt.observe()
    assert rt.act("click:e6").verified  # deleted, verified
    original = shop.observe
    calls = {"n": 0}

    def flaky_observe(*, cancel: Any = None) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OutcomeUnknownError("connection lost")
        return original(cancel=cancel)

    shop.observe = flaky_observe  # type: ignore[method-assign]
    with pytest.raises(OutcomeUnknownError):
        rt.observe()
    rt.observe()  # reconnected
    assert shop.actions.count("click Delete account") == 1


def test_stale_element_after_reconnect_is_not_clicked(app: App) -> None:
    shop = FakeShop()
    rt = _runtime(app, shop, RecordingUI())
    rt.observe()
    shop.controls[5].visible = False  # the page reloaded without that control
    with pytest.raises(NotFoundError, match="no longer on the screen"):
        rt.act("click:e6")
    assert "click Delete account" not in shop.actions
