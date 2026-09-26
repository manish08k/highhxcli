from highhx.core.executor import Executor
from highhx.core.result import Status
from highhx.execution.command import CommandSpec
from highhx.execution.process import ProcessOutcome
from highhx.execution.retry import RetryPolicy
from tests.conftest import py_cmd


class ScriptedRunner:
    def __init__(self, outcomes: list[ProcessOutcome]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def __call__(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        return outcome


def test_retries_with_exponential_backoff() -> None:
    sleeps: list[float] = []
    runner = ScriptedRunner(
        [ProcessOutcome(1, "", "boom", 0.01), ProcessOutcome(1, "", "", 0.01), ProcessOutcome(0, "ok", "", 0.01)]
    )
    executor = Executor(runner=runner, sleeper=sleeps.append)
    result = executor.execute(CommandSpec(["x"], retry=RetryPolicy(attempts=5, delay=1, backoff=3)))
    assert result.ok
    assert result.attempts == 3
    assert sleeps == [1, 3]


def test_gives_up_after_attempts() -> None:
    runner = ScriptedRunner([ProcessOutcome(2, "", "", 0.01)])
    result = Executor(runner=runner, sleeper=lambda _s: None).execute(CommandSpec(["x"], retry=RetryPolicy(attempts=3)))
    assert result.status == Status.FAILED
    assert result.exit_code == 2
    assert runner.calls == 3


def test_missing_command_is_not_retried() -> None:
    runner = ScriptedRunner([ProcessOutcome(127, "", "", 0.0, error="command not found: x")])
    result = Executor(runner=runner, sleeper=lambda _s: None).execute(CommandSpec(["x"], retry=RetryPolicy(attempts=5)))
    assert runner.calls == 1
    assert result.status == Status.FAILED


def test_timeout_status_and_message() -> None:
    result = Executor().execute(CommandSpec(py_cmd("import time; time.sleep(10)"), timeout=0.3))
    assert result.status == Status.TIMEOUT
    assert result.error == "timed out after 0.3s"


def test_missing_working_directory(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = Executor().execute(CommandSpec(["echo"], cwd=tmp_path / "missing"))
    assert result.status == Status.FAILED
    assert "does not exist" in (result.error or "")


def test_events_are_emitted() -> None:
    executor = Executor()
    names: list[str] = []
    executor.events.subscribe("*", lambda e: names.append(e.name))
    executor.execute(CommandSpec(py_cmd("pass")))
    assert names == ["command.started", "command.finished"]
