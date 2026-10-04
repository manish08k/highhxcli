"""Benchmarks: the task format, deterministic environments, the runner (real executor and agent
loop), the reported metrics, aggregation over repeated runs, storage and comparison — and a
model-planned task with a mock LLM."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from highhx.agent.model.capabilities import ModelCapabilities
from highhx.benchmarks import (
    METRICS,
    BenchmarkApprover,
    BenchmarkResult,
    BenchmarkRunner,
    BenchmarkStore,
    BenchmarkSuite,
    BenchmarkTask,
    aggregate,
    builtin_suites,
    compare,
    load_suite,
)
from highhx.benchmarks.model import RunMetrics, validate_task
from highhx.core.errors import NotFoundError, ValidationError
from highhx.models import ChatLanguageModel
from tests.unit.agent.conftest import ScriptedProvider, reply


def test_only_the_requested_metrics_are_reported() -> None:
    assert METRICS == (
        "success",
        "grounding_accuracy",
        "selector_healing_rate",
        "recovery_success_rate",
        "verification_accuracy",
        "average_retries",
        "completion_time",
        "token_cost",
        "tool_cost",
    )
    assert not any("failure" in m for m in METRICS)


def test_every_builtin_suite_loads() -> None:
    suites = builtin_suites()
    assert {"browser", "desktop", "android", "code", "long_horizon"} <= set(suites)
    for name in suites:
        suite = load_suite(name)
        assert suite.tasks and all(t.id for t in suite.tasks)
    with pytest.raises(NotFoundError):
        load_suite("nope")


def test_task_validation() -> None:
    assert validate_task({"id": "x", "environment": {"kind": "tv"}})
    assert validate_task({"id": "x", "planner": {"kind": "scripted"}})
    assert validate_task({"id": "x", "risk_level": "extreme"})
    assert validate_task({"id": "x", "success": {"bogus": 1}})
    assert validate_task({"id": "x", "environment": {"kind": "web"}, "planner": {"kind": "scripted", "steps": []}}) == []
    with pytest.raises(ValidationError):
        BenchmarkTask.from_dict({"environment": {"kind": "web"}})


def test_the_approver_declines_above_the_task_risk() -> None:
    approver = BenchmarkApprover("medium")

    class Request:
        def __init__(self, risk: str) -> None:
            self.risk_name = risk

    assert approver.confirm_action(Request("low")) and approver.confirm_action(Request("medium"))
    assert not approver.confirm_action(Request("high")) and not approver.confirm_action(Request("dangerous"))
    assert not approver.confirm_typed("x", "y") and approver.declined == 3


def test_the_browser_suite_measures_grounding_healing_recovery_and_safety() -> None:
    result = BenchmarkRunner().run_suite(load_suite("browser"))
    runs = {r.task_id: r for r in result.runs}
    assert all(r.success for r in result.runs), {r.task_id: r.summary for r in result.runs}
    redesign = runs["export-after-redesign"]
    assert redesign.selector_healing_rate == 1.0 and redesign.grounding_accuracy == 1.0
    fold = runs["export-below-the-fold"]
    assert fold.recovery_success_rate == 1.0 and fold.average_retries >= 2 and fold.selector_healing_rate is None
    declined = runs["declined-delete-is-never-done"]
    assert declined.status == "failed" and declined.verification_accuracy == 1.0  # not claimed, not achieved
    plain = runs["export-invoices"]
    assert plain.recovery_success_rate is None and plain.average_retries == 0 and plain.tool_cost > 0
    summary = result.to_dict()["summary"]
    assert summary["task_success_rate"] == 1.0 and summary["verification_accuracy"] == 1.0


@pytest.mark.parametrize("suite", ["desktop", "android", "code", "long_horizon"])
def test_other_suites_pass_deterministically(suite: str) -> None:
    result = BenchmarkRunner(runs=2).run_suite(load_suite(suite))
    assert len(result.runs) == 2 * len(load_suite(suite).tasks)
    assert all(r.success for r in result.runs), {r.task_id: r.summary for r in result.runs}
    assert {r.verification_accuracy for r in result.runs} == {1.0}


def test_a_wrong_claim_lowers_verification_accuracy(tmp_path: Path) -> None:
    # the agent is told nothing to check, so it claims done — but the evaluator sees the goal was not reached
    task = BenchmarkTask.from_dict(
        {
            "id": "false-claim",
            "environment": {"kind": "web"},
            "planner": {"kind": "scripted", "steps": [{"action": "open", "parameters": {"url": "https://shop.test/invoices"}}]},
            "evaluate": [{"state": {"exported": True}}],
        }
    )
    metrics = BenchmarkRunner().run_task(task)
    assert metrics.status == "completed" and not metrics.success and metrics.verification_accuracy == 0.0


def test_model_planned_tasks_use_an_injected_model_and_count_tokens() -> None:
    from highhx.agent.messages import Usage

    task = BenchmarkTask.from_dict(
        {
            "id": "model-export",
            "environment": {"kind": "web", "url": "https://shop.test/invoices"},
            "planner": {"kind": "model"},
            "success": {"text": "Export ready"},
            "evaluate": [{"state": {"exported": True}}],
            "allowed_tools": ["browser.", "computer.state"],
        }
    )
    caps = ModelCapabilities("local", "mock-planner", False, 0, 32000, "json", "pixels", True)

    def factory(_app: Any) -> ChatLanguageModel:
        return ChatLanguageModel(
            ScriptedProvider(
                [
                    reply('{"steps": ["export"]}'),
                    reply('{"action": {"action": "click", "target": {"label": "Export", "role": "button"}}}', usage=Usage(400, 20)),
                    reply('{"done": true, "summary": "exported"}', usage=Usage(500, 10)),
                ]
            ),
            caps,
        )

    skipped = BenchmarkRunner().run_suite(BenchmarkSuite("m", "", [task]))
    assert skipped.runs == []  # model tasks need a model: skipped, never faked
    result = BenchmarkRunner(model_factory=factory).run_suite(BenchmarkSuite("m", "", [task]))
    (run,) = result.runs
    assert run.success and run.provider == "model" and run.token_cost >= 930


def test_aggregation_leaves_out_rates_that_do_not_apply() -> None:
    def run(task: str, success: bool, healing: float | None, time: float) -> RunMetrics:
        return RunMetrics("b", task, 1, "web", "local", "scripted", success, 1.0, healing, None, 1.0, 1.0, time, 10, 3)

    stats = aggregate([run("a", True, 1.0, 1.0), run("a", False, None, 3.0)])
    assert stats["task_success_rate"] == 0.5 and stats["selector_healing_rate"] == 1.0
    assert stats["recovery_success_rate"] is None and stats["completion_time"]["mean"] == 2.0
    assert stats["completion_time"]["stdev"] > 0 and stats["token_cost"] == 20 and stats["tool_cost"] == 6


def test_results_are_stored_and_compared(tmp_path: Path) -> None:
    store = BenchmarkStore(tmp_path)
    with pytest.raises(NotFoundError):
        store.load()
    base = BenchmarkRunner().run_suite(load_suite("code"))
    store.save(base)
    other = BenchmarkResult.from_dict(base.to_dict())
    other.benchmark_id = "bench_other"
    other.runs[0].success = False
    store.save(other)
    assert store.load().benchmark_id == "bench_other" and store.ids()[-1] == "bench_other"
    diff = compare(base, other)
    assert diff["summary"]["task_success_rate"]["delta"] == -0.5 and diff["summary"]["task_success_rate"]["better"] is False
    assert set(diff["tasks"]) == {"write-config", "run-a-script"}


def test_doctor_reports_computer_use_capabilities_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    from highhx.core.result import CheckStatus
    from highhx.diagnostics.doctor import computer_use_checks

    monkeypatch.setenv("HIGHHX_ADB", "/nonexistent/adb")
    checks = {c.name: c for c in computer_use_checks()}
    android = checks["android driver unavailable"]
    assert android.status == CheckStatus.SKIP and "adb" in (android.hint or "")
    assert checks["vm driver unavailable"].status == CheckStatus.SKIP
    assert any(name.startswith(("sandbox isolation", "no sandbox")) for name in checks)
