"""The HighhX Computer Runtime's actions through the action executor: risk and approval,
verification of what can be observed, honesty about what cannot, and the terminal guard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from highhx.actions.catalog import default_catalog
from highhx.actions.policy import Approval, Risk
from highhx.cloud.plans import AGENT_COMPUTER_USE, PLANS, PRO
from tests.unit.automation.fakes import FakeEngine

CATALOG = default_catalog()
NEW = (
    "computer.observe",
    "computer.screenshot",
    "computer.windows",
    "computer.apps",
    "computer.element_at",
    "computer.click_at",
    "computer.move",
    "computer.drag",
    "computer.menu",
    "computer.window",
    "computer.quit",
    "computer.clipboard_read",
    "computer.clipboard_write",
)


def test_risk_floors_make_input_always_ask() -> None:
    asked = {
        "computer.click_at",
        "computer.drag",
        "computer.menu",
        "computer.quit",
        "computer.clipboard_read",
        "computer.clipboard_write",
    }
    for name in NEW:
        spec = CATALOG.get(name)
        assert spec is not None and spec.feature == AGENT_COMPUTER_USE, name
        assert not spec.agent, f"{name}: UI actions are never offered through run_actions"
        assert (spec.risk >= Risk.MEDIUM) == (name in asked), name


def test_run_actions_still_never_carries_ui_actions() -> None:
    offered = {s.name for s in CATALOG.for_agent(PLANS[PRO].features)}
    assert not offered & set(NEW)


def test_a_click_at_a_point_asks_and_a_no_means_nothing_happens(
    agent_project: Path, executor_for, engine: FakeEngine
) -> None:
    executor, ui = executor_for(agent_project)
    planned = executor.plan("computer.click_at", {"x": 110, "y": 110})
    assert planned.decision.risk == Risk.MEDIUM and planned.decision.approval == Approval.ASK
    ui.default_action_answer = False  # the person says no to the confirmation
    refused = executor.run("computer.click_at", {"x": 110, "y": 110})
    assert refused.status == "denied" and engine.sent("click_at") == []
    assert ui.requests and "(110, 110)" in str(ui.requests[-1].action)  # the exact point was shown
    ui.default_action_answer = True
    clicked = executor.run("computer.click_at", {"x": 110, "y": 110, "count": 2})
    assert clicked.ok and clicked.verified is None  # the effect belongs to the application: not claimed
    assert clicked.output["element"]["name"] == "Save" and clicked.summary == "double-clicked button 'Save'"


def test_click_on_text_is_grounded_by_perception(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    executor, _ = executor_for(agent_project)
    result = executor.run("computer.click_at", {"text": "Delete"})
    assert result.ok and engine.sent("click_at")[-1][1] == {"x": 240, "y": 115, "button": "left", "count": 1}
    assert result.output["grounded"]["source"] == "accessibility"
    missing = executor.run("computer.click_at", {"text": "Publish"})
    assert not missing.ok and "is not on the screen" in missing.error


def test_move_and_window_frames_are_verified(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    executor, _ = executor_for(agent_project)
    moved = executor.run("computer.move", {"x": 30, "y": 40})
    assert moved.ok and moved.verified and moved.output["cursor"] == [30, 40]
    framed = executor.run("computer.window", {"app": "Notes", "x": 0, "y": 25, "width": 600, "height": 400})
    assert framed.ok and framed.verified and framed.output["frame"] == [0, 25, 600, 400]
    engine.min_width = 500
    limited = executor.run("computer.window", {"window": 7, "x": 0, "y": 25, "width": 300, "height": 400})
    assert not limited.ok and limited.verified is False and "limits its size" in limited.error


def test_quitting_is_checked(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    executor, _ = executor_for(agent_project)
    engine.refuse_quit.add("Notes")
    stuck = executor.run("computer.quit", {"app": "Notes"})
    assert not stuck.ok and stuck.verified is False and "asking to save" in stuck.error
    engine.refuse_quit.clear()
    assert executor.run("computer.quit", {"app": "Notes"}).verified is True and "Notes" not in engine.running


def test_the_clipboard_is_read_back_after_writing(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    executor, _ = executor_for(agent_project)
    written = executor.run("computer.clipboard_write", {"text": "hello"})
    assert written.ok and written.verified and engine.clipboard == "hello"
    assert executor.run("computer.clipboard_read", {}).output["text"] == "hello"


def test_menus_never_reach_a_terminal(
    agent_project: Path, executor_for, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("highhx.computer.desktop._platform_key", lambda: "darwin")
    executor, _ = executor_for(agent_project)
    saved = executor.run("computer.menu", {"app": "Notes", "path": "File > Save"})
    assert saved.ok and engine.chosen == [["File", "Save"]] and saved.verified is None
    pasted = executor.run("computer.menu", {"app": "Terminal", "path": "Edit > Paste"})
    assert not pasted.ok and "terminal" in pasted.error and engine.chosen == [["File", "Save"]]
    missing = executor.run("computer.menu", {"app": "Notes", "path": "File > Export"})
    assert not missing.ok and "Export" in missing.error


def test_observation_actions(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    executor, _ = executor_for(agent_project)
    observed = executor.run("computer.observe", {"screenshot": True})
    assert observed.ok and observed.output["elements"][1]["name"] == "Delete"
    assert Path(observed.output["screenshot"]["path"]).is_file()
    shot = executor.run("computer.screenshot", {})
    assert shot.ok and shot.verified and shot.output["scale"] == 2.0
    listed = executor.run("computer.windows", {})
    assert listed.ok and listed.output["windows"][0]["id"] == 7
    apps = executor.run("computer.apps", {})
    assert {a["name"] for a in apps.output["apps"]} == {"Notes", "Finder"}
    at = executor.run("computer.element_at", {"x": 210, "y": 110})
    assert at.ok and at.output["name"] == "Delete"
    nothing = executor.run("computer.element_at", {"x": 5000, "y": 5000})
    assert not nothing.ok


def test_background_typing_names_the_target(
    agent_project: Path, executor_for, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("highhx.computer.desktop._platform_key", lambda: "darwin")
    executor, _ = executor_for(agent_project)
    typed = executor.run("computer.type", {"text": "note", "app": "Notes"})
    assert (
        typed.ok and typed.output["background"] and engine.sent("type") == [("type", {"text": "note", "app": "Notes"})]
    )
    refused = executor.run("computer.type", {"text": "rm -rf ~", "app": "Terminal"})
    assert not refused.ok and "never types" in refused.error


def test_desktop_scroll_uses_the_wheel_at_a_point(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    executor, _ = executor_for(agent_project)
    result = executor.run("computer.scroll", {"source": "desktop", "direction": "left", "x": 5, "y": 6, "amount": 2})
    assert result.ok and engine.sent("scroll") == [("scroll", {"direction": "left", "amount": 2, "x": 5, "y": 6})]


def test_desktop_elements_get_double_right_click_hover_and_drag(
    agent_project: Path, executor_for, engine: FakeEngine
) -> None:
    from highhx.automation.engine.bridge import AutomationBridge
    from highhx.automation.engine.provider import BridgeDesktopProvider
    from highhx.computer.driver import HighhXDriver

    provider = BridgeDesktopProvider(HighhXDriver(AutomationBridge(engine)))
    observation = provider.observe()
    save, delete = (e.id for e in observation.elements)
    provider.double_click(save)
    provider.right_click(save)
    provider.hover(delete)
    provider.drag(save, "Delete")
    calls: list[tuple[str, dict[str, Any]]] = engine.sent("click_at", "move", "drag")
    assert calls == [
        ("click_at", {"x": 140, "y": 115, "button": "left", "count": 2}),
        ("click_at", {"x": 140, "y": 115, "button": "right", "count": 1}),
        ("move", {"x": 240, "y": 115}),
        ("drag", {"from_x": 140, "from_y": 115, "to_x": 240, "to_y": 115, "button": "left", "duration_ms": 300}),
    ]
    engine.elements[0].pop("bounds")
    provider.observe()
    with pytest.raises(Exception, match="reports no position"):
        provider.double_click(save)
