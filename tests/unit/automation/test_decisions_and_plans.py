"""Deterministic decisions, JSON action plans (schema, validation, determinism), the plan runner (stop on
failure, verification decides success) and run traces/metrics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.catalog import default_catalog
from highhx.actions.policy import Risk
from highhx.actions.resolver import ResolverContext
from highhx.actions.spec import ActionResult
from highhx.cloud.capabilities import Capability
from highhx.decision.deterministic import DeterministicDecider
from highhx.decision.risk import risk_class
from highhx.language.targets import default_registry
from highhx.observability.runs import Metrics, RunStore, RunTrace
from highhx.plans.runner import PlanRunner
from highhx.plans.schema import PLAN_SCHEMA, PLAN_VERSION, ActionPlan, PlanError, validate_plan
from highhx.safety.actions import ActionKind
from highhx.storage.database import Database

FAKE_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"  # highhx:allow-secret (test fixture)


@pytest.fixture
def decider(tmp_path: Path) -> DeterministicDecider:
    (tmp_path / "hello.py").write_text("print('hello')\n")
    (tmp_path / "README.md").write_text("# demo\n")
    (tmp_path / "src").mkdir()
    return DeterministicDecider(ResolverContext(root=tmp_path, _targets=default_registry()))


# ------------------------------------------------------------------ decisions
@pytest.mark.parametrize(
    ("request_text", "intent", "target", "risk", "executor", "verification"),
    [
        ("open Gmail", "open", "gmail", "safe", "browser", "page_open"),
        ("open YouTube", "open", "youtube", "safe", "browser", "page_open"),
        ("open GitHub", "open", "github", "safe", "browser", "page_open"),
        ("open github.com", "open", "github", "safe", "browser", "page_open"),
        ("open Google Drive", "open", "google-drive", "safe", "browser", "page_open"),
        ("open my calendar", "open", "google-calendar", "safe", "browser", "page_open"),
        ("open Chrome", "launch", "chrome", "safe", "desktop", "app_running"),
        ("open Safari", "launch", "safari", "safe", "desktop", "app_running"),
        ("open Terminal", "launch", "terminal", "safe", "desktop", "app_running"),
        ("open VS Code", "launch", "vscode", "safe", "desktop", "app_running"),
        ("open Slack", "launch", "slack", "safe", "desktop", "app_running"),
        ("open Finder", "launch", "finder", "safe", "desktop", "app_running"),
        ("switch to Slack", "focus", "slack", "safe", "desktop", "app_frontmost"),
        ("open my project", "open", "project", "safe", "desktop", "opened_by_os"),
        ("open the readme", "open", "readme", "safe", "desktop", "opened_by_os"),
        ("open hello.py", "open", "hello.py", "safe", "desktop", "opened_by_os"),
        ("show my files", "list", "project", "safe", "filesystem", "listing"),
        ("list files in src", "list", "src", "safe", "filesystem", "listing"),
        ("list files in the source folder", "list", "source", "safe", "filesystem", "listing"),
        ("search YouTube for lofi", "search", "youtube", "safe", "browser", "search_results"),
        ("press cmd+t", "hotkey", "", "controlled", "desktop", "keys_sent"),
        ("scroll down", "scroll", "", "safe", "browser", "none"),
        ("show git status", "status", "project", "safe", "git", "command_captured"),
        ("check git changes", "diff", "project", "safe", "git", "command_captured"),
        ("run the tests", "test", "project", "controlled", "project", "process_exit"),
        ("create a folder called test", "create", "test", "controlled", "filesystem", "file_exists"),
        ("create a file called notes.md", "create", "notes.md", "controlled", "filesystem", "file_exists"),
        ("run python hello.py", "run", "hello.py", "controlled", "shell", "process_exit"),
    ],
)
def test_single_step_decisions(
    decider: DeterministicDecider,
    request_text: str,
    intent: str,
    target: str,
    risk: str,
    executor: str,
    verification: str,
) -> None:
    decision = decider.decide(request_text)
    assert decision.route == "local" and decision.supported, decision.reason
    plan = decision.plan
    assert plan is not None and len(plan.steps) == 1
    step = plan.steps[0]
    assert (plan.intent, plan.target, plan.risk, step.executor, step.verification) == (
        intent,
        target,
        risk,
        executor,
        verification,
    )


@pytest.mark.parametrize(
    ("request_text", "steps"),
    [
        ("play lofi on YouTube", ["search", "play"]),
        ("open YouTube and play Adhento Gani", ["open", "search", "play"]),
        ("open Gmail and search internship", ["open", "search"]),
        ("open Google and search for Python jobs", ["open", "search"]),
        ("open Chrome and search Google for Python jobs", ["search"]),
        ("open my project and run the tests", ["open", "test"]),
        ("open youtube, then play lofi and after that press space", ["open", "search", "play", "press"]),
    ],
)
def test_multi_step_workflows_stay_local(decider: DeterministicDecider, request_text: str, steps: list[str]) -> None:
    decision = decider.decide(request_text)
    assert decision.route == "local", "several deterministic steps are not a reason to escalate"
    assert decision.plan is not None and [s.action for s in decision.plan.steps] == steps
    assert decision.plan.intent == ("workflow" if len(steps) > 1 else steps[0])
    assert [s.id for s in decision.plan.steps] == [f"step_{n}" for n in range(1, len(steps) + 1)]


def test_the_gmail_workflow_plan(decider: DeterministicDecider) -> None:
    plan = decider.decide("open Gmail and search internship").plan
    assert plan is not None
    data = plan.to_dict()
    assert data["version"] == PLAN_VERSION and data["target"] == "gmail" and data["risk"] == "safe"
    assert data["steps"][1]["params"] == {"query": "internship", "site": "Gmail"}
    assert data["verification"] == {"type": "search_results"}


@pytest.mark.parametrize(
    "request_text",
    [
        "find the most important email from last week and draft a response",
        "debug this authentication system and fix all failing tests",
        "refactor this project and improve the architecture",
        "fix the failing tests",
        "search my emails for the invoice from last week",
        "make this project production ready",
    ],
)
def test_open_ended_requests_escalate_to_pro(decider: DeterministicDecider, request_text: str) -> None:
    decision = decider.decide(request_text)
    assert decision.route == "pro" and not decision.supported and decision.plan is None
    assert isinstance(decision.capability, Capability) and decision.reason


def test_unknown_entities_are_explained(decider: DeterministicDecider) -> None:
    decision = decider.decide("open spotifyy")
    assert decision.route == "unknown" and decision.plan is None
    assert decision.unknown is not None and "spotifyy" in decision.unknown.reason


def test_decisions_are_deterministic_and_serialisable(decider: DeterministicDecider) -> None:
    first = json.dumps(decider.decide("open YouTube and play Adhento Gani").to_dict(), sort_keys=True)
    second = json.dumps(decider.decide("open YouTube and play Adhento Gani").to_dict(), sort_keys=True)
    assert first == second
    data = json.loads(first)
    assert data["decision"] == "deterministic" and data["entities"]["queries"] == ["Adhento Gani"]
    assert data["clauses"] == ["open YouTube", "play Adhento Gani"]


def test_typed_text_is_counted_not_copied(decider: DeterministicDecider) -> None:
    decision = decider.decide("open slack and type my secret password")
    assert decision.plan is not None
    assert "my secret password" not in json.dumps(decision.entities)


def test_risk_classes() -> None:
    assert risk_class(Risk.SAFE) == "safe"
    assert risk_class(Risk.LOW, ActionKind.APP_LAUNCH) == "safe"
    assert risk_class(Risk.LOW, ActionKind.WRITE_FILE) == "controlled"
    assert risk_class(Risk.LOW, ActionKind.EXEC) == "controlled"
    assert risk_class(Risk.MEDIUM) == "controlled"
    assert risk_class(Risk.HIGH) == risk_class(Risk.CRITICAL) == "high"


def test_plan_risk_comes_from_the_executor_classifier(tmp_path: Path) -> None:
    """A plan never shows a lower risk than execution will apply."""
    critical = DeterministicDecider(
        ResolverContext(root=tmp_path, _targets=default_registry()), risk_of=lambda action, params: Risk.CRITICAL
    )
    plan = critical.decide("show git status").plan
    assert plan is not None and plan.risk == "high" and plan.risk_level == "critical"


# ---------------------------------------------------------------- JSON schema
def test_plans_round_trip_and_validate(decider: DeterministicDecider) -> None:
    catalog = default_catalog()
    for text in ("open Gmail and search internship", "open my project and run the tests", "press cmd+t"):
        plan = decider.decide(text).plan
        assert plan is not None
        again = ActionPlan.from_json(plan.to_json())
        assert again == plan and validate_plan(again, catalog) == []


def test_the_json_schema_describes_generated_plans(decider: DeterministicDecider) -> None:
    """Every generated plan fits PLAN_SCHEMA: required keys, no extra keys, enum values."""
    step_schema = PLAN_SCHEMA["steps"] if "steps" in PLAN_SCHEMA else PLAN_SCHEMA["properties"]["steps"]["items"]
    for text in ("open YouTube and play Adhento Gani", "open my project and run the tests", "switch to Slack"):
        plan = decider.decide(text).plan
        assert plan is not None
        data = plan.to_dict()
        assert set(PLAN_SCHEMA["required"]) <= set(data) <= set(PLAN_SCHEMA["properties"])
        assert data["risk"] in PLAN_SCHEMA["properties"]["risk"]["enum"]
        assert data["executor"] in PLAN_SCHEMA["properties"]["executor"]["enum"]
        for step in data["steps"]:
            assert set(step_schema["required"]) <= set(step) <= set(step_schema["properties"])
            assert step["executor"] in step_schema["properties"]["executor"]["enum"]
            assert step["risk_level"] in step_schema["properties"]["risk_level"]["enum"]


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda d: d.update(version="2"), "version"),
        (lambda d: d["steps"][0].update(catalog_action="shell.eval"), "unknown action"),
        (lambda d: d["steps"][0].update(action="delete"), "is 'open', not 'delete'"),
        (lambda d: d["steps"][0].update(executor="shell"), "runs on 'browser'"),
        (lambda d: d["steps"][0]["params"].update(evil=True), "unknown field"),
        (lambda d: d["steps"][0].update(params={}), "is required"),
        (lambda d: d.update(risk="none"), "risk"),
        (lambda d: d.update(steps=[]), "at least one step"),
    ],
)
def test_tampered_plans_are_rejected(decider: DeterministicDecider, change: Any, problem: str) -> None:
    plan = decider.decide("open Gmail and search internship").plan
    assert plan is not None
    data = plan.to_dict()
    change(data)
    problems = validate_plan(ActionPlan.from_dict(data), default_catalog())
    assert any(problem in p for p in problems), problems


def test_malformed_plans_raise() -> None:
    with pytest.raises(PlanError):
        ActionPlan.from_json("[]")
    with pytest.raises(PlanError):
        ActionPlan.from_json('{"version": "1"}')


# -------------------------------------------------------------------- runner
def _results(*results: ActionResult | None) -> Any:
    queue = list(results)
    seen: list[str] = []

    def execute(step: Any) -> ActionResult | None:
        seen.append(step.id)
        return queue.pop(0)

    return execute, seen


def test_the_runner_stops_at_the_first_failure(decider: DeterministicDecider, tmp_path: Path) -> None:
    plan = decider.decide("open YouTube and play Adhento Gani").plan
    assert plan is not None
    execute, seen = _results(
        ActionResult(True, output={"url": "https://www.youtube.com/", "title": "YouTube"}, verified=True),
        ActionResult(False, error="YouTube did not show search results"),
    )
    outcome = PlanRunner(execute, root=tmp_path).run(plan)
    assert not outcome.ok and outcome.status == "failed" and seen == ["step_1", "step_2"]
    assert [s.status for s in outcome.steps] == ["succeeded", "failed", "skipped"]
    assert outcome.failed_step is not None and outcome.failed_step.step.id == "step_2"
    assert "YouTube did not show search results" in outcome.reason
    assert outcome.verification == "failed"


def test_verification_decides_success(decider: DeterministicDecider, tmp_path: Path) -> None:
    plan = decider.decide("open Gmail").plan
    assert plan is not None
    # the action "succeeded", but the browser is somewhere else: not reported as done
    execute, _ = _results(ActionResult(True, output={"url": "https://example.com/"}))
    outcome = PlanRunner(execute, root=tmp_path).run(plan)
    assert not outcome.ok and "verification failed" in outcome.steps[0].error

    create = decider.decide("create a file called new.txt").plan
    assert create is not None
    execute, _ = _results(ActionResult(True, output={"path": "new.txt", "kind": "file"}))
    assert not PlanRunner(execute, root=tmp_path).run(create).ok  # it claims success, the file is not there
    (tmp_path / "new.txt").write_text("")
    execute, _ = _results(ActionResult(True, output={"path": "new.txt", "kind": "file"}))
    outcome = PlanRunner(execute, root=tmp_path).run(create)
    assert outcome.ok and outcome.verification == "verified"


def test_unobservable_steps_are_reported_as_unverified(decider: DeterministicDecider, tmp_path: Path) -> None:
    plan = decider.decide("press cmd+t").plan
    assert plan is not None
    execute, _ = _results(ActionResult(True, output={"app": "Google Chrome", "keys": "cmd+t"}))
    outcome = PlanRunner(execute, root=tmp_path).run(plan)
    assert outcome.ok and outcome.verification == "unverified"


def test_denied_steps_stop_the_plan(decider: DeterministicDecider, tmp_path: Path) -> None:
    plan = decider.decide("create a folder called build2 and run the tests").plan
    assert plan is not None
    execute, seen = _results(ActionResult(False, status="denied", error="not approved"))
    outcome = PlanRunner(execute, root=tmp_path).run(plan)
    assert [s.status for s in outcome.steps] == ["denied", "skipped"] and seen == ["step_1"]


# -------------------------------------------------------------------- traces
def test_runs_are_traced_redacted_and_measured(decider: DeterministicDecider, tmp_path: Path) -> None:
    from highhx.security.secrets import Redactor

    db = Database.open(tmp_path / "history.db")
    decision = decider.decide(f"open slack and type {FAKE_TOKEN}")
    assert decision.plan is not None
    trace = RunTrace(decision, db=db, redactor=Redactor(), source="test")
    execute, _ = _results(
        ActionResult(True, verified=True, output={"app": "Slack"}),
        ActionResult(True, output={"app": "Slack", "characters": 40}),
    )
    PlanRunner(execute, root=tmp_path, trace=trace).run(decision.plan)
    stored = RunStore(db).get(trace.run_id)
    assert stored is not None and stored["status"] == "succeeded" and stored["route"] == "local"
    assert FAKE_TOKEN not in json.dumps(stored)
    assert stored["steps"][1]["params"]["text"] == "<40 characters>"

    for text in ("refactor this project and improve the architecture", "open spotifyy"):
        RunTrace(decider.decide(text), db=db).finish()
    failed = decider.decide("open Gmail")
    execute, _ = _results(ActionResult(True, output={"url": "https://accounts.google.com/"}))
    PlanRunner(execute, root=tmp_path, trace=RunTrace(failed, db=db)).run(failed.plan)  # type: ignore[arg-type]
    metrics = RunStore(db).metrics().to_dict()
    assert metrics["total_runs"] == 4
    assert (metrics["successful_runs"], metrics["failed_runs"]) == (1, 1)
    assert (metrics["pro_escalations"], metrics["unknown_requests"]) == (1, 1)
    assert metrics["verification_failures"] == 1 and metrics["action_failures"] == {"browser.open": 1}
    assert {"target": "slack", "runs": 1} in metrics["most_used_targets"]
    assert RunStore(db).get(trace.run_id[-4:]) is not None  # found by a unique suffix
    db.close()


def test_metrics_of_nothing() -> None:
    assert Metrics.of([]).to_dict()["total_runs"] == 0
