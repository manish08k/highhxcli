"""HighhX Pro's agent on the desktop: direct operations through computer_act run as catalog
actions *as the agent* — the person is asked, a "no" means nothing is sent, and observations
give the model element positions to aim at."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from highhx.agent.tools.base import ToolError
from highhx.agent.tools.computer import DESKTOP_OPERATIONS, desktop_operation
from highhx.automation.engine.bridge import AutomationBridge
from highhx.computer.driver import HighhXDriver
from highhx.computer.session import ComputerSession
from tests.unit.agent.conftest import RecordingUI, reply
from tests.unit.automation.fakes import FakeEngine


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    fake = FakeEngine()
    driver = HighhXDriver(AutomationBridge(fake))
    monkeypatch.setattr(ComputerSession, "driver", lambda self: driver)
    return fake


def _results(provider: Any) -> list[str]:
    return [r.content for m in provider.requests[-1].messages for r in (m.tool_results or [])]


def test_the_agent_observes_positions_and_clicks_at_one_with_approval(
    agent_project: Path, make_session: Any, desktop: FakeEngine
) -> None:
    steps = [
        reply("", [("computer_observe", {"source": "desktop"})]),
        reply("", [("computer_act", {"source": "desktop", "action": "click_at:140,115"})]),
        reply("Clicked Save."),
    ]
    session, provider, ui = make_session(agent_project, steps)
    result = session.run_turn("click save")
    assert result.stopped == "completed"
    observed, clicked = _results(provider)
    assert "Positions in desktop points" in observed and "(100, 100, 80, 30)" in observed
    assert desktop.sent("click_at") == [("click_at", {"x": 140, "y": 115, "button": "left", "count": 1})]
    assert ui.requests and "(140, 115)" in str(ui.requests[-1].action)  # the person was asked first
    assert "computer.click_at" in clicked and "button 'Save'" in clicked


def test_a_no_means_the_agent_did_nothing(agent_project: Path, make_session: Any, desktop: FakeEngine) -> None:
    ui = RecordingUI(default_action_answer=False)
    steps = [
        reply("", [("computer_act", {"source": "desktop", "action": "drag:1,2,3,4"})]),
        reply("", [("computer_act", {"source": "desktop", "action": "clipboard_read"})]),
        reply("The user declined."),
    ]
    session, provider, _ = make_session(agent_project, steps, ui=ui)
    session.run_turn("move the file and read the clipboard")
    assert desktop.sent("drag", "clipboard_read") == []
    assert all("denied" in r or "Declined" in r or "not approved" in r.lower() for r in _results(provider))


def test_menus_quit_and_windows_need_the_application(
    agent_project: Path, make_session: Any, desktop: FakeEngine
) -> None:
    steps = [
        reply("", [("computer_act", {"source": "desktop", "action": "menu:File > Save", "app": "Notes"})]),
        reply("", [("computer_act", {"source": "desktop", "action": "window:0,25,640,480", "app": "Notes"})]),
        reply("Done."),
    ]
    session, _provider, _ = make_session(agent_project, steps)
    session.run_turn("save and resize")
    assert desktop.chosen == [["File", "Save"]]
    assert desktop.sent("window_frame")[-1][1] == {"window": 7, "x": 0, "y": 25, "width": 640, "height": 480}


def test_operation_ids_are_parsed_strictly() -> None:
    assert desktop_operation("click:e12", None, None) is None  # a runtime candidate, not a direct operation
    assert desktop_operation("right_click_at:1,2", None, None) == (
        "computer.click_at",
        {"x": 1, "y": 2, "button": "right"},
    )
    assert desktop_operation("scroll_at:5,6,up", None, None) == (
        "computer.scroll",
        {"source": "desktop", "direction": "up", "x": 5, "y": 6},
    )
    assert desktop_operation("click_text:Save", None, "Notes") == (
        "computer.click_at",
        {"text": "Save", "app": "Notes"},
    )
    for bad in ("click_at:1", "drag:1,2,3", "move:a,b", "quit", "menu:File > Save"):
        with pytest.raises(ToolError):
            desktop_operation(bad, None, None)
    assert set(DESKTOP_OPERATIONS) >= {"click_at", "drag", "menu", "hotkey", "quit", "window", "screenshot"}
