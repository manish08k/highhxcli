"""The unified action protocol: aliases, the semantic risk floor, outcome classification,
bounded retries, declarative verification and the trace envelope — all through the one executor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from highhx.actions import events as ev
from highhx.actions.catalog import Catalog, default_catalog
from highhx.actions.policy import Risk
from highhx.actions.protocol import ActionRequest, Outcome, prepare, resolve, submit, to_node
from highhx.actions.spec import ActionResult, ActionSpec
from highhx.core.errors import ValidationError
from highhx.execution.retry import RetryPolicy
from highhx.perception.state import ComputerState, StateElement
from highhx.safety.actions import ActionKind, Actor
from highhx.utils.validation import Int, Obj, Prop, Str


def _catalog(calls: list[tuple[str, dict[str, Any]]], *, fail_times: int = 0) -> Catalog:
    state = {"failures": 0}

    def click(_ctx: Any, inputs: dict[str, Any]) -> ActionResult:
        calls.append(("click", inputs))
        return ActionResult(True, summary="clicked", verified=None)

    def flaky(_ctx: Any, inputs: dict[str, Any]) -> ActionResult:
        calls.append(("flaky", inputs))
        if state["failures"] < fail_times:
            state["failures"] += 1
            return ActionResult(False, error="transient")
        return ActionResult(True, summary="read")

    def poke(_ctx: Any, inputs: dict[str, Any]) -> ActionResult:
        calls.append(("poke", inputs))
        return ActionResult(False, error="nope")

    point = Obj({"x": Prop(Int()), "y": Prop(Int()), "count": Prop(Int())})
    return Catalog(
        [
            *default_catalog(),
            ActionSpec("test.click", "click at a point", click, point, risk=Risk.LOW, kind=ActionKind.UI_CLICK),
            ActionSpec("test.read", "read something", flaky, Obj({"q": Prop(Str())}), risk=Risk.SAFE),
            ActionSpec("test.poke", "a risky write", poke, Obj({}), risk=Risk.MEDIUM, kind=ActionKind.WRITE_FILE),
        ]
    )


def test_aliases_resolve_to_catalog_actions() -> None:
    assert resolve("desktop.click", {"x": 1, "y": 2}) == ("computer.click_at", {"x": 1, "y": 2})
    assert resolve("desktop.double_click", {"x": 1, "y": 2})[1]["count"] == 2
    assert resolve("desktop.scroll", {"direction": "down"})[1] == {"source": "desktop", "direction": "down"}
    assert resolve("shell.exec", {"command": "ls"}) == ("shell.run", {"command": "ls"})
    assert resolve("filesystem.read", {"path": "a"}) == ("filesystem.read", {"path": "a"})
    for alias in (a for a in __import__("highhx.actions.protocol", fromlist=["x"]).ACTION_ALIASES):
        name, _ = resolve(alias, {})
        assert name in default_catalog(), alias


def test_requests_round_trip_and_redact_typed_text() -> None:
    request = ActionRequest(
        "desktop.type",
        {"text": "hunter2"},
        intent="log in",
        target={"label": "Password", "role": "textbox"},
        grounding=({"strategy": "accessibility", "result": "success"},),
        retry_policy=RetryPolicy(2, 0.1),
        min_risk=Risk.HIGH,
        trace_id="tr_1",
    )
    again = ActionRequest.from_dict(request.to_dict())
    assert again == request
    assert request.redacted()["parameters"]["text"] == "<7 characters>"
    child = request.child("desktop.key", {"key": "enter"})
    assert child.parent_action == request.id and child.id != request.id and child.trace_id == "tr_1"
    node = to_node(request)
    assert node.action == "computer.type" and node.id == request.id


def test_a_semantic_label_raises_the_risk_of_a_coordinate_click(agent_project: Path, executor_for) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    executor, ui = executor_for(agent_project, catalog=_catalog(calls))
    plain = prepare(executor, ActionRequest("test.click", {"x": 10, "y": 20}))
    assert plain.risk == Risk.LOW
    labelled = prepare(executor, ActionRequest("test.click", {"x": 10, "y": 20}, target={"label": "Delete account"}))
    assert labelled.risk >= Risk.HIGH and labelled.approval.name == "ASK"
    assert any("Delete account" in r for r in labelled.planned.decision.reasons)
    ui.action_answers = [False]  # the person declines: nothing is clicked
    response = submit(executor, ActionRequest("test.click", {"x": 10, "y": 20}, target={"label": "Delete account"}))
    assert response.outcome == Outcome.FAILED and response.status == "denied" and calls == []


def test_min_risk_only_raises(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project, catalog=_catalog([]))
    assert prepare(executor, ActionRequest("test.read", {"q": "x"}, min_risk=Risk.HIGH)).risk == Risk.HIGH
    assert prepare(executor, ActionRequest("test.poke", {}, min_risk=Risk.SAFE)).risk == Risk.MEDIUM


def test_ui_actions_without_verification_are_unknown_not_success(agent_project: Path, executor_for) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    executor, _ = executor_for(agent_project, catalog=_catalog(calls))
    response = submit(executor, ActionRequest("test.click", {"x": 1, "y": 1}))
    assert response.result.ok and response.outcome == Outcome.UNKNOWN and not response.ok


def test_declarative_verification_classifies_the_outcome(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project, catalog=_catalog([]))
    before = ComputerState("browser", elements=(StateElement("e1", "button", "Submit"),))
    after_done = ComputerState("browser", elements=(StateElement("e2", "text", "Thank you"),), text="Thank you")
    request = ActionRequest(
        "test.click",
        {"x": 1, "y": 1},
        verification={"all": [{"absent": {"role": "button", "name": "Submit"}}, {"text": "Thank you"}]},
    )
    ok = submit(executor, request, before=before, observe=lambda: after_done)
    assert ok.outcome == Outcome.SUCCESS and ok.verification is not None and ok.verification.satisfied
    half = ComputerState("browser", elements=(StateElement("e3", "button", "Submit"),), text="Thank you")
    partial = submit(executor, request, before=before, observe=lambda: half)
    assert partial.outcome == Outcome.PARTIAL_SUCCESS
    unknown = submit(executor, ActionRequest("test.click", {"x": 1, "y": 1}, verification={"network": {"status": 200}}))
    assert unknown.outcome == Outcome.UNKNOWN


def test_safe_actions_are_retried_and_risky_ones_never(agent_project: Path, executor_for) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    executor, _ = executor_for(agent_project, catalog=_catalog(calls, fail_times=2))
    response = submit(executor, ActionRequest("test.read", {"q": "x"}, retry_policy=RetryPolicy(3)), sleep=lambda _s: None)
    assert response.outcome == Outcome.SUCCESS and response.attempts == 3 and len(calls) == 3
    calls.clear()
    risky = submit(executor, ActionRequest("test.poke", {}, retry_policy=RetryPolicy(5)), sleep=lambda _s: None)
    assert risky.outcome == Outcome.FAILED and risky.attempts == 1 and len(calls) == 1


def test_events_carry_the_trace_and_action_ids(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project, catalog=_catalog([]))
    seen: list[Any] = []
    executor.events.subscribe("*", seen.append)
    request = ActionRequest("test.read", {"q": "x"}, trace_id="tr_abc")
    submit(executor, request)
    names = [e.name for e in seen]
    assert ev.ACTION_PLANNED in names and ev.ACTION_COMPLETED in names
    assert all(e.context.get("trace_id") == "tr_abc" and e.context.get("action_id") == request.id for e in seen)


def test_unknown_actions_and_bad_inputs_raise_before_anything_runs(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project, catalog=_catalog([]))
    from highhx.actions.executor import UnknownActionError

    with pytest.raises(UnknownActionError):
        submit(executor, ActionRequest("nope.nope"))
    with pytest.raises(ValidationError):
        submit(executor, ActionRequest("test.click", {"x": "left"}))


def test_the_agent_actor_is_asked_even_for_low_ui_actions(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project, actor=Actor.AGENT, catalog=_catalog([]))
    submit(executor, ActionRequest("test.click", {"x": 1, "y": 1}))
    assert ui.of("permission")
