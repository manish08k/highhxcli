"""Each benchmark metric, computed from a hand-built trajectory whose right answer is known —
and shown to move with the data, so nothing can be a constant. Only the requested metrics exist."""

from __future__ import annotations

from typing import Any

import pytest

from highhx.benchmarks import METRICS, BenchmarkTask, aggregate
from highhx.benchmarks.model import RunMetrics
from highhx.benchmarks.runner import run_metrics
from highhx.benchmarks.store import COMPARED
from highhx.trajectories import Trajectory, TrajectoryStep


def step(
    index: int,
    intent: str,
    outcome: str,
    *,
    label: str = "",
    grounding: dict[str, Any] | None = None,
    decision: str = "continue",
) -> TrajectoryStep:
    return TrajectoryStep(
        index,
        intent,
        {"action_type": "browser.click", "target": {"label": label}, "step": {"intent": intent}},
        {"outcome": outcome},
        {},
        None,
        {"decision": decision, "reason": ""},
        grounding,
    )


def grounded(strategy: str, name: str, attempts: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "candidate": {"strategy": strategy, "score": 0.9, "element": {"name": name}},
        "attempts": [{"strategy": s, "result": r} for s, r in attempts],
        "target": {"label": name, "semantic": {"label": name}},
    }


def trajectory(status: str, steps: list[TrajectoryStep], **metrics: Any) -> Trajectory:
    t = Trajectory("t", "browser", status=status)
    t.steps = steps
    t.metrics = {
        "recoveries": 0,
        "replans": 0,
        "seconds": 1.5,
        "tokens_in": 0,
        "tokens_out": 0,
        "actions": 0,
        "observations": 0,
        **metrics,
    }
    return t


def metrics(t: Trajectory, goal: bool, **task: Any) -> RunMetrics:
    spec = BenchmarkTask.from_dict(
        {"id": "x", "environment": {"kind": "web"}, "planner": {"kind": "scripted", "steps": []}, **task}
    )
    return run_metrics(spec, t, goal, benchmark_id="b", run=1, provider="scripted", runtime="local")


def test_task_success_and_verification_accuracy() -> None:
    done = trajectory("completed", [])
    assert metrics(done, True).success and metrics(done, True).verification_accuracy == 1.0
    false_claim = metrics(done, False)
    assert not false_claim.success and false_claim.verification_accuracy == 0.0
    gave_up = metrics(trajectory("failed", []), False)
    assert not gave_up.success and gave_up.verification_accuracy == 1.0  # it correctly did not claim success
    missed = metrics(trajectory("failed", []), True)
    assert not missed.success and missed.verification_accuracy == 0.0  # achieved but not recognised
    safety = metrics(trajectory("failed", []), False, expect_completion=False)
    assert safety.success  # a safety task succeeds by NOT completing


def test_grounding_accuracy_against_ground_truth_and_by_verification() -> None:
    steps = [
        step(
            1,
            "open",
            "success",
            label="Invoices",
            grounding=grounded("accessibility", "Invoices", [("accessibility", "success")]),
        ),
        step(
            2,
            "export",
            "success",
            label="Export",
            grounding=grounded("text", "Export all", [("accessibility", "success")]),
        ),
        step(
            3,
            "save",
            "failed",
            label="Save",
            grounding=grounded("accessibility", "Save", [("accessibility", "success")]),
        ),
    ]
    with_truth = metrics(
        trajectory("completed", steps), True, ground_truth={"Invoices": "Invoices", "Export": "Export"}
    )
    # Invoices right, Export wrong (grounded "Export all"), Save graded by its failed verification
    assert with_truth.grounding_accuracy == round(1 / 3, 4)
    without = metrics(trajectory("completed", steps), True)
    assert without.grounding_accuracy == round(2 / 3, 4)
    assert metrics(trajectory("completed", [step(1, "x", "success")]), True).grounding_accuracy is None


def test_selector_healing_rate_counts_only_drifted_selectors() -> None:
    healed = step(
        1,
        "export",
        "success",
        label="Export",
        grounding=grounded("dom", "Download CSV", [("accessibility", "failed"), ("dom", "success")]),
    )
    unhealed = step(
        2,
        "pay",
        "failed",
        label="Pay",
        grounding={
            "candidate": {},
            "attempts": [{"strategy": "accessibility", "result": "failed"}, {"strategy": "dom", "result": "failed"}],
        },
    )
    direct = step(
        3, "open", "success", label="Open", grounding=grounded("accessibility", "Open", [("accessibility", "success")])
    )
    ambiguous = step(
        4,
        "delete",
        "failed",
        label="Delete",
        grounding={"candidate": {}, "attempts": [{"strategy": "accessibility", "result": "ambiguous"}]},
    )
    m = metrics(trajectory("failed", [healed, unhealed, direct, ambiguous]), False)
    assert m.selector_healing_rate == 0.5  # 1 healed of 2 drifted; direct and ambiguous are not "healing needed"
    assert metrics(trajectory("completed", [direct]), True).selector_healing_rate is None


def test_recovery_success_rate_follows_later_steps_of_the_same_intent() -> None:
    steps = [
        step(1, "export", "failed", decision="scroll"),
        step(2, "export", "success"),
        step(3, "submit", "failed", decision="replan"),
        step(4, "submit", "failed", decision="replan"),
    ]
    m = metrics(trajectory("failed", steps, recoveries=1, replans=2), False)
    assert m.recovery_success_rate == round(1 / 3, 4)  # the scroll worked; the two re-plans did not
    assert m.average_retries == 3.0
    assert metrics(trajectory("completed", [step(1, "x", "success")]), True).recovery_success_rate is None


def test_time_and_cost_come_from_the_run() -> None:
    m = metrics(
        trajectory(
            "completed", [], seconds=12.3456, tokens_in=1200, tokens_out=80, actions=7, observations=9, cost=0.0123
        ),
        True,
    )
    assert m.completion_time == 12.346 and m.token_cost == 1280 and m.tool_cost == 16 and m.cost_usd == 0.0123
    cheaper = metrics(
        trajectory("completed", [], seconds=1.0, tokens_in=10, tokens_out=0, actions=1, observations=1), True
    )
    assert (cheaper.completion_time, cheaper.token_cost, cheaper.tool_cost) == (1.0, 10, 2)


def test_aggregates_are_exactly_the_requested_metrics() -> None:
    runs = [
        metrics(
            trajectory(
                "completed",
                [
                    step(
                        1,
                        "a",
                        "success",
                        label="A",
                        grounding=grounded("accessibility", "A", [("accessibility", "success")]),
                    )
                ],
                seconds=2.0,
            ),
            True,
        ),
        metrics(trajectory("failed", [], seconds=4.0, recoveries=2), True),
    ]
    stats = aggregate(runs)
    assert set(stats) == {
        "runs",
        "task_success_rate",
        "grounding_accuracy",
        "selector_healing_rate",
        "recovery_success_rate",
        "verification_accuracy",
        "average_retries",
        "completion_time",
        "token_cost",
        "tool_cost",
        "cost_usd",
    }
    assert (
        stats["task_success_rate"] == 0.5 and stats["verification_accuracy"] == 0.5 and stats["average_retries"] == 1.0
    )
    assert stats["grounding_accuracy"] == 1.0 and stats["selector_healing_rate"] is None
    assert stats["completion_time"]["mean"] == 3.0
    for name in (*METRICS, *COMPARED, *stats):
        assert "failure" not in name and "classif" not in name


@pytest.mark.parametrize("field", ["grounding_accuracy", "selector_healing_rate", "recovery_success_rate"])
def test_rates_that_do_not_apply_are_never_averaged_as_numbers(field: str) -> None:
    applies = RunMetrics("b", "t", 1, "web", "local", "s", True, 1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 0, 0)
    not_applicable = RunMetrics("b", "t", 2, "web", "local", "s", True, None, None, None, 1.0, 0.0, 1.0, 0, 0)
    assert aggregate([applies, not_applicable])[field] == 1.0  # not (1.0 + 0) / 2
