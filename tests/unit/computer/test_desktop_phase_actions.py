"""Desktop actions added in this phase — window maximize/minimize, OS-aware edit shortcuts, wait —
through the real executor, driver and automation bridge on the simulated desktop."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from highhx.actions.executor import ActionExecutor
from highhx.automation.engine.bridge import AutomationBridge
from highhx.computer.driver import HighhXDriver
from highhx.computer.session import ComputerSession
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.computer_use.environment import SimulatedDesktop
from tests.unit.agent.conftest import RecordingUI

DESKTOP = Path(__file__).resolve().parents[2] / "computer_use" / "tasks" / "_desktop.yaml"


@pytest.fixture
def desk(agent_project: Path, make_app, monkeypatch: pytest.MonkeyPatch) -> Any:
    env = SimulatedDesktop(yaml.safe_load(DESKTOP.read_text()))
    driver = HighhXDriver(AutomationBridge(env))
    monkeypatch.setattr(ComputerSession, "driver", lambda self: driver)
    app = make_app(agent_project)
    ui = RecordingUI()
    gate = ActionGate(app.engine, ui, source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    executor = ActionExecutor(app, gate, actor=Actor.USER, sleep=lambda _s: None)
    return env, executor, ui


def test_maximize_is_verified_by_the_new_frame(desk: Any) -> None:
    env, executor, _ = desk
    app_name = env.front
    result = executor.run("computer.window_state", {"state": "maximize", "app": app_name})
    assert result.ok and result.verified and result.output["frame"] == [0, 0, 1440, 900]


def test_window_actions_reach_a_running_app_the_registry_does_not_list(desk: Any) -> None:
    # Regression: an application started from a bare binary (no .app bundle, not a known target)
    # was refused by name ("I don't know an application called ...") although it was on screen.
    env, executor, _ = desk
    env.apps["HighhXFixture"] = env.apps.pop("Notes")
    env.apps["HighhXFixture"]["window"]["app"] = "HighhXFixture"
    env.front = "HighhXFixture"
    result = executor.run("computer.window_state", {"state": "maximize", "app": "HighhXFixture"})
    assert result.ok and result.verified and result.output["app"] == "HighhXFixture"
    missing = executor.run("computer.window_state", {"state": "maximize", "app": "NoSuchAppAnywhere"})
    assert not missing.ok and "not on screen" in missing.error


@pytest.mark.skipif(sys.platform != "darwin", reason="minimizing uses the macOS Window menu")
def test_minimize_is_verified_by_the_window_leaving_the_screen(desk: Any) -> None:
    env, executor, _ = desk
    app_name = env.front
    result = executor.run("computer.window_state", {"state": "minimize", "app": app_name})
    assert result.ok and result.verified
    assert env.apps[app_name]["window"]["minimized"] is True
    again = executor.run("computer.window_state", {"state": "maximize", "app": app_name})
    assert not again.ok and "not on screen" in again.error  # nothing guessed about a hidden window


def test_edit_shortcuts_use_the_platform_modifier_and_paste_is_asked(desk: Any) -> None:
    _, executor, _ui = desk
    result = executor.run("computer.edit", {"op": "select_all"})
    modifier = "cmd" if sys.platform == "darwin" else "ctrl"
    assert result.ok and result.output["keys"] == f"{modifier}+a" and result.output["op"] == "select_all"
    assert executor.plan("computer.edit", {"op": "copy"}).decision.risk == 1
    paste = executor.plan("computer.edit", {"op": "paste"})
    assert paste.decision.risk == 2 and paste.decision.asks  # pasting types into the application: asked


def test_edit_never_reaches_a_terminal(desk: Any) -> None:
    _, executor, _ = desk
    result = executor.run("computer.edit", {"op": "paste", "app": "Terminal"})
    assert not result.ok and "terminal" in result.error.lower()


def test_wait_is_bounded_and_cancellable(desk: Any) -> None:
    _, executor, _ = desk
    from highhx.core.errors import ValidationError

    assert executor.run("computer.wait", {"seconds": 0}).ok
    with pytest.raises(ValidationError):
        executor.plan("computer.wait", {"seconds": 61})


def test_file_dialogs_are_refused_where_they_cannot_be_driven(
    desk: Any, monkeypatch: pytest.MonkeyPatch, agent_project: Path
) -> None:
    from highhx.actions.catalog import default_catalog
    from highhx.actions.policy import Risk
    from highhx.safety.actions import ActionKind

    _env, executor, _ui = desk
    (agent_project / "a.txt").write_text("x")
    spec = next(s for s in default_catalog() if s.name == "computer.file_dialog")
    assert (
        spec.risk == Risk.MEDIUM
        and spec.kind_for is not None
        and spec.kind_for({"kind": "save"}) == ActionKind.WRITE_FILE
    )
    if sys.platform == "darwin":
        none_open = executor.run("computer.file_dialog", {"path": "a.txt"})
        assert not none_open.ok and "no file dialog is open" in none_open.error  # the simulated desktop has none
    monkeypatch.setattr(sys, "platform", "linux")
    elsewhere = executor.run("computer.file_dialog", {"path": "a.txt"})
    assert not elsewhere.ok and "driven on macOS" in elsewhere.error
