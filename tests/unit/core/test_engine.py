import pytest

from highhx.approvals.policy import ApprovalPolicy
from highhx.approvals.risk import RiskLevel
from highhx.core.errors import ApprovalDeniedError, CommandFailedError, PolicyViolationError
from highhx.core.result import Status
from highhx.execution.command import CommandSpec
from highhx.policy.engine import PolicySet
from tests.conftest import py_cmd


def test_normal_command_runs_and_is_recorded(make_engine) -> None:  # type: ignore[no-untyped-def]
    kit = make_engine()
    result = kit.engine.run(CommandSpec(py_cmd("print('hello')")))
    assert result.ok
    record = kit.history.latest()
    assert record is not None and record.status == "success" and record.kind == "command"
    assert any("hello" in line for line in kit.logs.read(record.id))


def test_dry_run_never_executes(make_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    marker = tmp_path / "marker"
    kit = make_engine(dry_run=True)
    result = kit.engine.run(CommandSpec(py_cmd(f"open({str(marker)!r}, 'w').write('x')")))
    assert result.dry_run and result.status == Status.SKIPPED
    assert not marker.exists()


def test_dangerous_command_denied_without_terminal(make_engine) -> None:  # type: ignore[no-untyped-def]
    kit = make_engine(interactive=False)
    with pytest.raises(ApprovalDeniedError):
        kit.engine.run(CommandSpec("git push origin main"))


def test_yes_approves_dangerous_but_not_non_bypassable(make_engine) -> None:  # type: ignore[no-untyped-def]
    kit = make_engine(interactive=False, yes=True)
    decision = kit.engine.approve("push", RiskLevel.DANGEROUS)
    assert decision.approved and decision.mode == "yes-flag"
    with pytest.raises(ApprovalDeniedError):
        kit.engine.approve("wipe", RiskLevel.CRITICAL, bypassable=False)


def test_yes_limit_from_policy(make_engine) -> None:  # type: ignore[no-untyped-def]
    kit = make_engine(interactive=False, yes=True, approval_policy=ApprovalPolicy(yes_max_risk=RiskLevel.DANGEROUS))
    with pytest.raises(ApprovalDeniedError):
        kit.engine.approve("deploy prod", RiskLevel.CRITICAL)


def test_interactive_prompt_is_used(make_engine) -> None:  # type: ignore[no-untyped-def]
    kit = make_engine(interactive=True, answer=False)
    with pytest.raises(ApprovalDeniedError):
        kit.engine.approve("push", RiskLevel.DANGEROUS)
    assert kit.prompter.asked


def test_policy_deny_blocks_even_with_yes(make_engine) -> None:  # type: ignore[no-untyped-def]
    policy = PolicySet.from_dict({"rules": [{"id": "no-curl", "when": {"command": "curl"}, "effect": "deny"}]})
    kit = make_engine(yes=True, policy=policy)
    with pytest.raises(PolicyViolationError):
        kit.engine.run(CommandSpec("curl https://example.invalid"))


def test_policy_require_approval_raises_risk(make_engine) -> None:  # type: ignore[no-untyped-def]
    policy = PolicySet.from_dict(
        {
            "rules": [
                {
                    "id": "gate",
                    "when": {"action": "deploy:*"},
                    "effect": "require_approval",
                    "risk": "critical",
                    "bypassable": False,
                }
            ]
        }
    )
    kit = make_engine(yes=True, interactive=False, policy=policy)
    with pytest.raises(ApprovalDeniedError):
        kit.engine.approve("Deploy", RiskLevel.NORMAL, policy_action="deploy:prod")


def test_check_raises_command_failed(make_engine) -> None:  # type: ignore[no-untyped-def]
    kit = make_engine()
    with pytest.raises(CommandFailedError):
        kit.engine.run(CommandSpec(py_cmd("import sys; sys.exit(5)")), check=True)


def test_secrets_are_redacted_in_logs_and_history(make_engine) -> None:  # type: ignore[no-untyped-def]
    kit = make_engine()
    secret = "s3cr3t-token-value-123456"  # highhx:allow-secret
    kit.engine.run(
        CommandSpec(py_cmd("import os; print('token is', os.environ['API_TOKEN'])"), env={"API_TOKEN": secret})
    )
    record = kit.history.latest()
    assert record is not None
    log = "\n".join(kit.logs.read(record.id))
    assert "token is" in log
    assert secret not in log


def test_nested_operations_become_steps(make_engine) -> None:  # type: ignore[no-untyped-def]
    kit = make_engine()
    with kit.engine.operation("release", "v1"), kit.engine.operation("git", "tag"):
        kit.engine.run(CommandSpec(py_cmd("pass"), name="inner"))
    record = kit.history.latest()
    assert record is not None and record.kind == "release"
    assert [s.step_id for s in record.steps] == ["inner", "git:tag"]


def test_failed_operation_records_error(make_engine) -> None:  # type: ignore[no-untyped-def]
    kit = make_engine()
    with pytest.raises(CommandFailedError), kit.engine.operation("build", "app"):
        kit.engine.run(CommandSpec(py_cmd("import sys; sys.exit(2)")), check=True)
    record = kit.history.latest()
    assert record is not None and record.status == "failed" and record.exit_code == 2
