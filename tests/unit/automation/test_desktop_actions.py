"""Desktop actions through the bridge: focus verifies the result, errors are explicit, and desktop
clicks keep the computer runtime's per-element safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.automation.fakes import FakeEngine


def test_switch_to_launches_focuses_and_verifies(
    agent_project: Path, executor_for, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("highhx.computer.desktop._platform_key", lambda: "darwin")  # macOS app names
    executor, _ = executor_for(agent_project)
    result = executor.run("computer.focus", {"app": "slack"})
    assert result.ok and result.verified and result.output["launched"]
    assert [op for op, _ in engine.calls if op in ("launch", "focus")] == ["launch", "focus"]
    assert engine.front == "Slack"


def test_switch_to_reports_when_the_app_did_not_come_forward(
    agent_project: Path, executor_for, engine: FakeEngine
) -> None:
    engine.running.add("Slack")
    engine.focus_fails = True
    executor, _ = executor_for(agent_project)
    result = executor.run("computer.focus", {"app": "Slack"})
    assert not result.ok and result.verified is False
    assert "Slack is not in front (Notes is)" in result.error


def test_missing_accessibility_is_an_error_not_a_success(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    engine.deny = "hotkey"
    executor, _ = executor_for(agent_project)
    result = executor.run("computer.hotkey", {"keys": "cmd+t"})
    assert not result.ok and "Accessibility" in result.error


def test_hotkeys_are_sent_as_structured_operations(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    executor, _ = executor_for(agent_project)
    result = executor.run("computer.hotkey", {"keys": "cmd+shift+t"})
    assert result.ok and result.output == {"app": "Notes", "keys": "cmd+shift+t"}
    assert engine.sent("hotkey") == [("hotkey", {"modifiers": ["command", "shift"], "key": "t"})]


def test_desktop_clicks_keep_per_element_safety(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    executor, ui = executor_for(agent_project)
    ui.default_action_answer = False  # decline every sensitive-element confirmation
    saved = executor.run("computer.click", {"target": "button:Save"})
    assert saved.ok and engine.sent("click") == [("click", {"name": "Save", "role": "button"})]
    engine.calls = []
    deleted = executor.run("computer.click", {"target": "button:Delete"})
    assert not deleted.ok and engine.sent("click") == []  # "Delete" asked, was declined, never clicked


def test_a_click_that_changes_nothing_is_not_reported_as_done(
    agent_project: Path, executor_for, engine: FakeEngine
) -> None:
    engine.clicks_change_ui = False
    executor, _ = executor_for(agent_project)
    result = executor.run("computer.click", {"target": "button:Save"})
    assert engine.sent("click") and not result.ok and result.verified is False
    assert "no visible change" in result.error
