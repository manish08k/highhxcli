"""Task IR and action validation: closed schemas, per-primitive rules, no way to run code."""

from __future__ import annotations

import json
from typing import Any

import pytest

from highhx.computer.model import Observation, UIElement
from highhx.core.errors import ValidationError
from highhx.goals.ir import (
    ACTIONS,
    MAX_NESTING,
    Action,
    Branch,
    Repeat,
    json_schemas,
    loads_json,
    parse_task,
    validate_action,
    validate_task,
)
from highhx.goals.targets import parse_target


def _action(**fields: Any) -> dict[str, Any]:
    return {"reason": "test", **fields}


# ------------------------------------------------------------- actions
@pytest.mark.parametrize(
    "data",
    [
        _action(action="browser.navigate", value="https://books.example/"),
        _action(action="browser.navigate", value="books.example"),
        _action(action="browser.click", target="button:Search"),
        _action(action="browser.click", target="e12"),
        _action(action="browser.type", target="searchbox:Search", value="dune"),
        _action(action="browser.press", value="enter"),
        _action(action="browser.scroll", value="down"),
        _action(action="browser.wait", value="2"),
        _action(action="browser.wait", expect={"text": "Results"}),
        _action(action="browser.verify", expect={"media_playing": True}),
        _action(action="browser.read", value=r"[\w.]+@[\w.]+"),
        _action(action="browser.new_tab"),
        _action(action="browser.back"),
        _action(action="task.done"),
        _action(action="task.ask_user", value="Which John?"),
    ],
)
def test_valid_actions(data: dict[str, Any]) -> None:
    assert validate_action(data) == []
    assert Action.from_dict(data).to_dict()["action"] == data["action"]


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (_action(action="shell.run", value="rm -rf /"), "is not one of"),
        (_action(action="browser.eval", value="document.cookie"), "is not one of"),
        (_action(action="python.exec", value="import os"), "is not one of"),
        ({"action": "browser.click", "target": "e1"}, "reason"),
        (_action(action="browser.click"), "needs a target"),
        (_action(action="browser.type", target="e1"), "needs a value"),
        (_action(action="browser.navigate", value="javascript:alert(1)"), "not a web address"),
        (_action(action="browser.navigate", value="file:///etc/passwd"), "not a web address"),
        (_action(action="browser.press", value="ctrl+w"), "key must be one of"),
        (_action(action="browser.scroll", value="sideways"), "up or down"),
        (_action(action="browser.wait", value="600"), "at most"),
        (_action(action="browser.wait"), "needs seconds"),
        (_action(action="browser.verify"), "needs a condition"),
        (_action(action="browser.read", value="(unclosed"), "invalid regular expression"),
        (_action(action="browser.click", target="e1", script="x"), "unknown field"),
        (_action(action="browser.click", target="e1", expect={}), "checks nothing"),
        (_action(action="browser.click", target="e1", expect={"eval": "1"}), "unknown field"),
        (_action(action="browser.click", target="[href"), "attribute filter"),
        (_action(action="task.fail"), "needs the reason"),
        ("click the button", "expected an object"),
    ],
)
def test_invalid_actions_are_rejected(data: Any, problem: str) -> None:
    errors = validate_action(data)
    assert errors and any(problem in e for e in errors), errors
    if isinstance(data, dict):
        with pytest.raises(ValidationError):
            Action.from_dict(data)


def test_no_primitive_runs_code() -> None:
    assert all(a.startswith(("browser.", "task.")) for a in ACTIONS)
    assert not any(word in a for a in ACTIONS for word in ("eval", "exec", "shell", "script", "run", "python"))


# ------------------------------------------------------------- tasks
def _task(**fields: Any) -> dict[str, Any]:
    return {"goal": "find the contact email", **fields}


def test_a_full_task_parses() -> None:
    task = parse_task(
        _task(
            constraints={"max_steps": 10, "timeout": 60, "stay_on_site": True},
            context={"start_url": "https://books.example/"},
            steps=[
                _action(action="browser.navigate", value="https://books.example/"),
                {"if": {"element": "button:Accept"}, "then": [_action(action="browser.click", target="button:Accept")]},
                {
                    "repeat": {
                        "steps": [_action(action="browser.scroll", value="down")],
                        "until": {"element": "link:Contact"},
                        "max": 5,
                    }
                },
                _action(action="browser.click", target="link:Contact", expect={"url_contains": "/contact"}),
                _action(action="browser.read", value=r"[\w.]+@[\w.]+"),
            ],
            success_conditions=[{"text_matches": r"@books\.example"}],
            failure_conditions=[{"text": "Access denied"}],
            allow_replanning=False,
        )
    )
    assert task.constraints.max_steps == 10 and task.constraints.stay_on_site
    assert isinstance(task.steps[1], Branch) and isinstance(task.steps[2], Repeat)
    assert task.steps[3].expect is not None and task.steps[3].expect.url_contains == "/contact"
    assert not task.allow_replanning and not task.dynamic
    again = parse_task(json.loads(json.dumps(task.to_dict())))
    assert again == task  # the IR round-trips


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        ({}, "goal"),
        (_task(), "otherwise nothing says when it is done"),
        (_task(steps=[_action(action="task.done")]), "planner decision, not a step"),
        (_task(steps=[{"repeat": {"steps": [], "until": {"text": "x"}, "max": 1000}}]), "<="),
        (_task(steps=[_action(action="browser.navigate", value="ftp://x")]), "not a web address"),
        (_task(success_conditions=[{}]), "checks nothing"),
        (_task(success_conditions=[{"text": "x"}], constraints={"timeout": 99999}), "at most"),
        (_task(success_conditions=[{"text": "x"}], context={"n": 1}), "expected a string"),
        (_task(success_conditions=[{"text": "x"}], tools=["shell"]), "unknown field"),
        ("not a task", "expected an object"),
    ],
)
def test_malformed_task_ir_is_rejected(data: Any, problem: str) -> None:
    errors = validate_task(data)
    assert errors and any(problem in e for e in errors), errors
    with pytest.raises(ValidationError) as info:
        parse_task(data)
    assert info.value.details == errors


def test_nesting_is_bounded() -> None:
    step: dict[str, Any] = _action(action="browser.scroll", value="down")
    for _ in range(MAX_NESTING + 1):
        step = {"if": {"text": "x"}, "then": [step]}
    assert validate_task(_task(steps=[step]))


def test_a_task_without_steps_is_dynamic() -> None:
    task = parse_task(_task(success_conditions=[{"text_matches": "@"}]))
    assert task.dynamic and task.allow_replanning


def test_json_from_models_and_files() -> None:
    assert loads_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert loads_json('Sure! {"a": 1} Hope that helps') == {"a": 1}
    with pytest.raises(ValidationError):
        loads_json("no json here")


def test_json_schemas_export() -> None:
    schemas = json_schemas()
    assert schemas["action"]["additionalProperties"] is False
    assert set(schemas["action"]["properties"]["action"]["enum"]) == set(ACTIONS)
    assert "task" not in json_schemas(compact=True)
    json.dumps(schemas)  # finite and serialisable


# ------------------------------------------------------------- targets
PAGE = Observation(
    "browser",
    "FakeWeb",
    "Results",
    "https://tube.example/search?q=x",
    [
        UIElement("e1", "searchbox", "Search", attributes={"placeholder": "Search videos"}),
        UIElement("e2", "link", "Home", attributes={"href": "/"}),
        UIElement("e3", "link", "First video", attributes={"href": "/watch?v=1"}),
        UIElement("e4", "link", "Second video", attributes={"href": "/watch?v=2"}),
        UIElement("e5", "button", "Hidden", visible=False),
    ],
    "",
)


@pytest.mark.parametrize(
    ("target", "found"),
    [
        ("e3", "e3"),
        ("Search", "e1"),
        ("link:Second", "e4"),
        ('link="Home"', "e2"),
        ("link#2", "e3"),
        ("link[href*=/watch]#1", "e3"),
        ("link[href*=/watch]#2", "e4"),
        ("[placeholder^=Search]", "e1"),
        ("link[href$=v=2]", "e4"),
        ("button:Hidden", None),
        ("link[href*=/nothing]", None),
        ("e99", None),
    ],
)
def test_targets_are_discovered_from_the_page(target: str, found: str | None) -> None:
    element = parse_target(target).find(PAGE)
    assert (element.id if element else None) == found


@pytest.mark.parametrize("target", ["", "   ", "[href", "x" * 400])
def test_bad_targets(target: str) -> None:
    with pytest.raises(ValueError):
        parse_target(target)
