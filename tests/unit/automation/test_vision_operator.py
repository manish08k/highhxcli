"""The vision agent end to end over a recording engine: UI-TARS/JSON answers → validated actions →
grounded coordinates (Retina/downscaled screenshots, newest-only, retired after acting, moved or
covered windows refused) → computer.* actions through the executor → the next screenshot.
The model is scripted; everything below it is HighhX's real code."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from highhx.agent.messages import ImageBlock, Message, TextBlock, Usage
from highhx.agent.model.capabilities import capabilities_for
from highhx.agent.streaming import Completed, TextDelta
from highhx.computer.capture import CaptureStore, StaleCapture
from highhx.computer.operator.computer import ComputerOperator
from highhx.computer.operator.parse import ActionParseError, parse_action, point_of
from highhx.computer.operator.vision import COMPLETED, FAILED, NEEDS_USER, VisionAgent
from highhx.core.errors import ModelProviderError
from highhx.execution.cancellation import CancellationToken
from tests.unit.automation.fakes import FakeEngine

NOTES = {"id": 7, "pid": 70, "app": "Notes", "title": "Untitled", "x": 0, "y": 25, "width": 800, "height": 600}


# ------------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    ("answer", "kind", "point", "extra"),
    [
        ("Thought: save it\nAction: click(start_box='(100,200)')", "click", (100, 200), {}),
        ("Action: left_double(start_box='<|box_start|>(10,20,30,40)<|box_end|>')", "double_click", (20, 30), {}),
        ("Action: right_single(start_box='[5, 6]')", "right_click", (5, 6), {}),
        ("Action: drag(start_box='(1,2)', end_box='(3,4)')", "drag", (1, 2), {"end": (3, 4)}),
        ("Action: hotkey(key='ctrl c')", "hotkey", None, {"keys": "ctrl+c"}),
        ("Action: press(key='enter')", "key", None, {"keys": "enter"}),
        ("Action: type(content='it\\'s done')", "type", None, {"text": "it's done"}),
        ("Action: scroll(start_box='(9,9)', direction='up')", "scroll", (9, 9), {"direction": "up"}),
        ("Action: finished(content='saved')", "finished", None, {"text": "saved"}),
        ('```json\n{"thought": "x", "action": "click", "x": 12, "y": 34}\n```', "click", (12, 34), {}),
        ('{"action": "open_app", "app": "TextEdit"}', "open_app", None, {"app": "TextEdit"}),
    ],
)
def test_answers_become_one_validated_action(answer: str, kind: str, point: Any, extra: dict[str, Any]) -> None:
    action = parse_action(answer)
    assert action.kind == kind and action.point == point
    for key, value in extra.items():
        assert getattr(action, key) == value


@pytest.mark.parametrize(
    "answer",
    [
        "",
        "I think I should click the button",
        "Action: rm_rf(start_box='(1,2)')",
        "Action: click()",
        "Action: drag(start_box='(1,2)')",
        '{"action": "type"}',
        '{"action": "click", "x": "left", "y": 1}',
        "Action: scroll(direction='sideways')",
    ],
)
def test_what_is_not_one_valid_action_is_refused(answer: str) -> None:
    with pytest.raises(ActionParseError):
        parse_action(answer)
    with pytest.raises(ActionParseError):
        point_of("(1,2,3)")


# ------------------------------------------------------------------ grounding
@pytest.fixture
def screen(executor_for: Any, agent_project: Path, engine: FakeEngine) -> tuple[Any, FakeEngine]:
    engine.shot_size = (720, 450)  # the 1440x900-point screen, captured at half size for a model
    engine.windows = [dict(NOTES)]
    executor, ui = executor_for(agent_project)
    ui.default_action_answer = True
    return executor, engine


def test_pixels_of_a_downscaled_screenshot_become_desktop_points(screen: tuple[Any, FakeEngine]) -> None:
    executor, engine = screen
    store: CaptureStore = executor.computer().captures
    capture = store.take(executor.driver())
    assert store.ground(executor.driver(), capture.id, 70, 57) == (140, 114)  # 2 points per pixel
    assert store.ground(executor.driver(), capture.id, 100, 200, space="relative1000") == (144, 180)
    with pytest.raises(StaleCapture, match="outside"):
        store.ground(executor.driver(), capture.id, 720, 10)


def test_only_the_newest_unused_screenshot_grounds_coordinates(screen: tuple[Any, FakeEngine]) -> None:
    executor, engine = screen
    store: CaptureStore = executor.computer().captures
    first = store.take(executor.driver())
    second = store.take(executor.driver())
    with pytest.raises(StaleCapture, match="older"):
        store.ground(executor.driver(), first.id, 10, 20)
    clicked = executor.run("computer.click_at", {"capture": second.id, "x": 70, "y": 57})
    assert clicked.ok and engine.sent("click_at")[-1][1]["x"] == 140
    again = executor.run("computer.click_at", {"capture": second.id, "x": 70, "y": 57})  # the click changed the screen
    assert not again.ok and again.error.startswith("Stale screenshot") and len(engine.sent("click_at")) == 1


def test_a_moved_or_covered_window_makes_the_screenshot_stale(screen: tuple[Any, FakeEngine]) -> None:
    executor, engine = screen
    store: CaptureStore = executor.computer().captures
    capture = store.take(executor.driver())
    engine.windows = [{**NOTES, "x": 300}]
    with pytest.raises(StaleCapture, match="moved|gone"):
        store.ground(executor.driver(), capture.id, 70, 57)
    capture = store.take(executor.driver())
    engine.windows = [{**NOTES, "id": 9, "app": "Mail"}, {**NOTES, "x": 300}]
    with pytest.raises(StaleCapture, match="Mail"):
        store.ground(executor.driver(), store.latest or capture.id, 70, 57)


# ------------------------------------------------------------------- the loop
class Scripted:
    """A model that answers from a list (and records what it was shown)."""

    name = "scripted"
    default_model = "scripted"

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.requests: list[Any] = []

    def stream(self, request: Any, *, cancel: Any = None) -> Iterator[Any]:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        yield TextDelta(answer)
        yield Completed(Message("assistant", [TextBlock(answer)]), "end_turn", Usage())


def run_agent(executor: Any, answers: list[Any], **kwargs: Any) -> tuple[Any, Scripted, list[str]]:
    model = Scripted(answers)
    events: list[str] = []
    caps = capabilities_for("local", "ui-tars-1.5-7b", config={"base_url": "http://127.0.0.1:8000/v1", "coordinates": "pixels"})
    agent = VisionAgent(
        ComputerOperator(executor),
        model,
        caps,
        cancel=CancellationToken(),
        emit=lambda name, **_d: events.append(name),
        sleep=lambda _s: None,
        **kwargs,
    )
    return agent.run("save the note"), model, events


def test_the_loop_sees_acts_through_the_executor_and_confirms_finished(screen: tuple[Any, FakeEngine]) -> None:
    executor, engine = screen
    result, model, events = run_agent(
        executor,
        [
            "Thought: the Save button\nAction: click(start_box='(70,57)')",
            "Action: hotkey(key='cmd s')",
            "Action: finished(content='saved')",
            "Action: finished(content='saved')",  # confirmed on a fresh screenshot
        ],
    )
    assert result.status == COMPLETED and result.reason == "saved"
    assert engine.sent("click_at")[0][1]["x"] == 140 and engine.sent("hotkey")[0][1]["key"] == "s"
    first = model.requests[0].messages[0]
    assert any(isinstance(b, ImageBlock) for b in first.blocks) and "Task: save the note" in first.text
    assert "computer.action.executed" in events and events[-1] == "computer.task.completed"
    assert len(engine.sent("screenshot")) == 4  # before each decision, and for the confirmation


def test_invalid_answers_are_explained_and_asked_again(screen: tuple[Any, FakeEngine]) -> None:
    executor, _engine = screen
    result, model, _ = run_agent(executor, ["click the button", "Action: finished()", "Action: finished()"])
    assert result.status == COMPLETED
    assert "not one valid action" in model.requests[1].messages[-1].text


def test_declined_actions_and_call_user_stop_for_the_person(screen: tuple[Any, FakeEngine]) -> None:
    executor, engine = screen
    result, _, _ = run_agent(executor, ["Action: call_user()"])
    assert result.status == NEEDS_USER
    executor.gate.prompter.default_action_answer = False  # the person declines the click
    declined, _, _ = run_agent(executor, ["Action: click(start_box='(70,57)')"])
    assert declined.status == NEEDS_USER and "denied" in declined.reason and engine.sent("click_at") == []


def test_model_errors_are_retried_then_reported(screen: tuple[Any, FakeEngine]) -> None:
    executor, _engine = screen
    flaky = ModelProviderError("overloaded", retryable=True)
    result, _, events = run_agent(executor, [flaky, "Action: finished()", "Action: finished()"])
    assert result.status == COMPLETED and "computer.recovery" in events
    down, _, _ = run_agent(executor, [flaky, flaky, flaky])
    assert down.status == FAILED and "unavailable" in down.reason


def test_the_same_action_over_and_over_is_stopped(screen: tuple[Any, FakeEngine]) -> None:
    executor, _engine = screen
    result, _, _ = run_agent(executor, ["Action: press(key='tab')"] * 5)
    assert result.status == FAILED and "no progress" in result.reason


def test_a_model_that_cannot_see_is_refused_before_anything_runs(screen: tuple[Any, FakeEngine]) -> None:
    executor, engine = screen
    blind = capabilities_for("local", "llama3-8b", config={"base_url": "http://127.0.0.1:8000/v1"})
    agent = VisionAgent(ComputerOperator(executor), Scripted([]), blind, cancel=CancellationToken())
    with pytest.raises(ModelProviderError, match="does not take images"):
        agent.run("anything")
    assert engine.sent("screenshot") == []
