"""Executor semantics: validation, approvals by risk and actor, retries, timeouts, cancellation,
verification, compensation, graphs, events, audit and history."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from highhx.actions import events as ev
from highhx.actions.catalog import Catalog, default_catalog
from highhx.actions.executor import ActionNode, GraphError, UnknownActionError, run_graph, validate_graph
from highhx.actions.policy import Risk
from highhx.actions.spec import ActionResult, ActionSpec
from highhx.core.errors import ValidationError
from highhx.execution.retry import RetryPolicy
from highhx.safety.actions import ActionKind, Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ApprovalMode
from highhx.utils.validation import Obj, Prop, Str


def recorded(app: Any) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    app.ctx.events.subscribe("*", lambda e: events.append((e.name, e.data)))
    return events


# ------------------------------------------------------------- validation
def test_unknown_actions_and_bad_inputs_never_run(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project)
    with pytest.raises(UnknownActionError):
        executor.run("nope.nope")
    with pytest.raises(ValidationError) as caught:
        executor.run("filesystem.write", {"path": "a.txt"})
    assert any("content" in d for d in caught.value.details)
    assert ui.events == []


def test_dry_run_plans_without_executing(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project, dry_run=True)
    result = executor.run("filesystem.write", {"path": "new.txt", "content": "x"})
    assert result.status == "planned" and result.ok and "risk: medium" in result.summary
    assert not (agent_project / "new.txt").exists() and ui.requests == []


# -------------------------------------------------------------- approvals
def test_safe_and_low_run_for_the_user_without_asking(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project)
    assert executor.run("filesystem.read", {"path": "pyproject.toml"}).ok
    assert executor.run("project.test").ok  # low
    assert ui.requests == [] and ui.of("permission") == []


def test_low_asks_the_agent(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project, actor=Actor.AGENT)
    assert executor.run("project.test").ok
    assert ui.of("permission") == ["project.test · highhx test"]


def test_low_runs_for_the_agent_in_auto_edit(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project, actor=Actor.AGENT, mode=ApprovalMode.AUTO_EDIT)
    assert executor.run("project.test").ok and ui.of("permission") == []


def test_medium_asks_with_the_five_level_label(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project)
    assert executor.run("filesystem.write", {"path": "a.txt", "content": "x\n"}).ok
    (request,) = ui.requests
    assert request.risk_name == "medium" and request.confirm_word is None and request.tool == "filesystem.write"


def test_declined_medium_does_nothing(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project)
    ui.action_answers = [False]
    result = executor.run("filesystem.write", {"path": "a.txt", "content": "x\n"})
    assert result.status == "denied" and not (agent_project / "a.txt").exists()


def test_critical_needs_a_typed_word(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project)
    ui.action_answers = [False]
    result = executor.run("shell.run", {"command": f"rm -rf {agent_project / 'build'}"})
    assert result.status == "denied"
    assert ui.requests[0].confirm_word == "approve" and ui.requests[0].risk_name == "critical"


def test_yes_covers_the_users_medium_and_high_but_never_critical(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project, yes=True, interactive=False)
    assert executor.run("filesystem.write", {"path": "a.txt", "content": "x\n"}).ok
    assert executor.run("filesystem.delete", {"path": "a.txt"}).ok  # high
    critical = executor.run("shell.run", {"command": f"rm -rf {agent_project / 'build'}"})
    assert critical.status == "denied" and ui.requests == []


def test_yes_never_covers_the_agent(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project, actor=Actor.AGENT, yes=True, interactive=False)
    result = executor.run("filesystem.delete", {"path": "pyproject.toml"})
    assert result.status == "denied" and (agent_project / "pyproject.toml").exists()


def test_preapproved_plans_follow_the_same_limits(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project)
    medium = executor.plan("filesystem.write", {"path": "a.txt", "content": "x\n"})
    assert executor.execute(medium, preapproved=True).ok and ui.requests == []
    critical = executor.plan("shell.run", {"command": f"rm -rf {agent_project / 'build'}"})
    ui.action_answers = [False]
    assert executor.execute(critical, preapproved=True).status == "denied"
    assert ui.requests[-1].confirm_word == "approve"
    agent, agent_ui = executor_for(agent_project, actor=Actor.AGENT)
    planned = agent.plan("filesystem.write", {"path": "b.txt", "content": "x\n"})
    agent.execute(planned, preapproved=True)
    assert agent_ui.requests  # the agent's actions always ask


def test_blocked_never_runs_even_with_yes(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project, yes=True)
    result = executor.run("shell.run", {"command": "rm -rf /"})
    assert result.status == "blocked" and ui.requests == []


def test_read_only_mode_refuses_changes(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project, actor=Actor.AGENT, mode=ApprovalMode.READ_ONLY)
    assert executor.run("filesystem.read", {"path": "pyproject.toml"}).ok
    assert executor.run("filesystem.write", {"path": "a.txt", "content": "x"}).status == "denied"


def test_project_policy_applies(agent_project: Path, executor_for) -> None:
    (agent_project / ".highhx" / "policies.yaml").write_text("forbidden_files: ['*.lock']\n")
    executor, _ = executor_for(agent_project)
    result = executor.run("filesystem.write", {"path": "deps.lock", "content": "x"})
    assert not result.ok and "forbidden_files" in result.error


# ------------------------------------------------------ retries / timeouts
def _catalog(*specs: ActionSpec) -> Catalog:
    return Catalog([*default_catalog(), *specs])


def _flaky(fail_times: int) -> tuple[Any, list[int]]:
    calls: list[int] = []

    def handler(ctx: Any, inputs: dict[str, Any]) -> ActionResult:
        calls.append(1)
        return ActionResult(len(calls) > fail_times, error="transient")

    return handler, calls


def test_idempotent_actions_retry_with_backoff(agent_project: Path, executor_for) -> None:
    handler, calls = _flaky(2)
    spec = ActionSpec("test.read", "reads", handler, idempotent=True, retry=RetryPolicy(3, delay=0.5, backoff=2.0))
    executor, _ = executor_for(agent_project, catalog=_catalog(spec))
    events = recorded(executor.app)
    result = executor.run("test.read")
    assert result.ok and result.attempts == 3 and len(calls) == 3
    retries = [d for name, d in events if name == ev.ACTION_RETRY]
    assert [r["delay"] for r in retries] == [0.5, 1.0]


def test_non_idempotent_actions_are_never_retried(agent_project: Path, executor_for) -> None:
    handler, calls = _flaky(1)
    spec = ActionSpec("test.write", "writes", handler, risk=Risk.LOW, idempotent=False, retry=RetryPolicy(5, delay=0.1))
    executor, _ = executor_for(agent_project, catalog=_catalog(spec))
    result = executor.run("test.write")
    assert not result.ok and len(calls) == 1 and result.attempts == 1


def test_timeouts_cancel_the_handler(agent_project: Path, executor_for) -> None:
    def slow(ctx: Any, inputs: dict[str, Any]) -> ActionResult:
        ctx.cancel.wait(5)
        from highhx.core.errors import OperationCancelledError

        raise OperationCancelledError("stopped")

    spec = ActionSpec("test.slow", "slow", slow, timeout=0.2)
    executor, _ = executor_for(agent_project, catalog=_catalog(spec))
    result = executor.run("test.slow")
    assert result.status == "timeout" and result.seconds < 3


def test_cancellation(agent_project: Path, executor_for) -> None:
    started = threading.Event()

    def wait(ctx: Any, inputs: dict[str, Any]) -> ActionResult:
        started.set()
        from highhx.core.errors import OperationCancelledError

        if ctx.cancel.wait(5):
            raise OperationCancelledError("cancelled")
        return ActionResult(True)

    spec = ActionSpec("test.wait", "waits", wait)
    executor, _ = executor_for(agent_project, catalog=_catalog(spec))
    from highhx.execution.cancellation import CancellationToken

    token = CancellationToken()
    threading.Thread(target=lambda: (started.wait(2), token.cancel("user"))).start()
    assert executor.run("test.wait", cancel=token).status == "cancelled"


# ------------------------------------------------ verification / audit / events
def test_verification_failure_fails_the_action(agent_project: Path, executor_for) -> None:
    spec = ActionSpec(
        "test.unverified",
        "claims success",
        lambda ctx, i: ActionResult(True, summary="done"),
        verify=lambda ctx, i, r: (False, "the file is not there"),
    )
    executor, _ = executor_for(agent_project, catalog=_catalog(spec))
    result = executor.run("test.unverified")
    assert not result.ok and result.verified is False and "the file is not there" in result.error


def test_everything_is_audited_recorded_and_emitted(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    events = recorded(executor.app)
    executor.run("filesystem.write", {"path": "a.txt", "content": "x\n"})
    names = [n for n, _ in events if n.startswith(("action.", "approval."))]
    assert names == [
        ev.ACTION_PLANNED,
        ev.APPROVAL_REQUESTED,
        ev.APPROVAL_GRANTED,
        ev.ACTION_STARTED,
        ev.ACTION_COMPLETED,
    ]
    audit = AuditLog(executor.app.db, executor.app.redactor).list(limit=5)
    assert audit and audit[0].tool == "filesystem.write" and audit[0].status == "ok"
    history = executor.app.history.list(kind="action", limit=5)
    assert history and history[0].name == "filesystem.write" and history[0].status == "success"


def test_failed_actions_are_recorded_as_failed(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    result = executor.run("filesystem.read", {"path": "missing.txt"})
    assert result.status == "failed" and "does not exist" in result.error
    assert executor.app.history.list(kind="action", limit=1)[0].status == "failed"


# ------------------------------------------------------------------ graphs
def test_graph_validation(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    with pytest.raises(GraphError, match="unique"):
        validate_graph(executor, [ActionNode("a", "git.status"), ActionNode("a", "git.diff")])
    with pytest.raises(GraphError, match="unknown step"):
        validate_graph(executor, [ActionNode("a", "git.status", depends_on=("z",))])
    with pytest.raises(GraphError, match="cycle"):
        validate_graph(
            executor, [ActionNode("a", "git.status", depends_on=("b",)), ActionNode("b", "git.diff", depends_on=("a",))]
        )
    with pytest.raises(ValidationError):  # every input is checked before anything runs
        validate_graph(executor, [ActionNode("a", "project.test"), ActionNode("b", "filesystem.write", {})])


def test_graph_stops_skips_and_rolls_back(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    nodes = [
        ActionNode("one", "filesystem.write", {"path": "one.txt", "content": "1"}),
        ActionNode("two", "filesystem.write", {"path": "two.txt", "content": "2"}, depends_on=("one",)),
        ActionNode("boom", "filesystem.read", {"path": "missing.txt"}, depends_on=("two",)),
        ActionNode("never", "filesystem.write", {"path": "three.txt", "content": "3"}, depends_on=("boom",)),
    ]
    outcome = run_graph(executor, nodes, rollback=True)
    assert not outcome.ok and outcome.stopped_at == "boom"
    assert "never" not in outcome.results
    assert [c.split(":")[0] for c in outcome.compensations] == ["filesystem.write", "filesystem.write"]
    assert "two.txt" in outcome.compensations[0]  # newest first
    assert not (agent_project / "one.txt").exists() and not (agent_project / "two.txt").exists()


def test_continue_on_error(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    nodes = [
        ActionNode("boom", "filesystem.read", {"path": "missing.txt"}, continue_on_error=True),
        ActionNode("after", "filesystem.read", {"path": "pyproject.toml"}, depends_on=("boom",)),
    ]
    outcome = run_graph(executor, nodes)
    assert outcome.ok and outcome.results["after"].ok
    assert outcome.results["boom"].attempts == 1  # a deterministic failure is not retried


def test_compensation_of_denied_or_failed_actions_is_a_no_op(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    planned = executor.plan("filesystem.write", {"path": "a.txt", "content": "x"})
    assert executor.compensate(planned, ActionResult(False, status="denied")) is None


def test_custom_spec_inputs_are_validated(agent_project: Path, executor_for) -> None:
    spec = ActionSpec(
        "test.named",
        "needs a name",
        lambda c, i: ActionResult(True),
        Obj({"name": Prop(Str(min_length=1), required=True)}),
    )
    executor, _ = executor_for(agent_project, catalog=_catalog(spec))
    with pytest.raises(ValidationError):
        executor.run("test.named", {})
    assert executor.run("test.named", {"name": "x"}).ok


def test_kind_drives_classification(agent_project: Path, executor_for) -> None:
    spec = ActionSpec("test.deploy", "deploys", lambda c, i: ActionResult(True), kind=ActionKind.DEPLOY)
    executor, _ = executor_for(agent_project, catalog=_catalog(spec))
    assert executor.plan("test.deploy").decision.risk == Risk.HIGH  # the classifier sees a deployment
