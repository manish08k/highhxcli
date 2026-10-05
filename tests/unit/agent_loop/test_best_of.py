"""Best-of-N: independent attempts (project copies), an evaluator that checks each copy itself,
early stop on a perfect score, budgets, cleanup, the winner as a diff; never on a shared world."""

from __future__ import annotations

from pathlib import Path

import pytest

from highhx.agent.loop import AgentTask, ScriptedPlanner
from highhx.agent.loop.best_of import BestOfN, project_copies
from highhx.core.errors import UsageError
from tests.unit.agent.conftest import RecordingUI, agent_project, make_app  # noqa: F401


def planners(contents: list[str]):  # type: ignore[no-untyped-def]
    return lambda number: ScriptedPlanner(
        [{"action": "filesystem.write", "parameters": {"path": "answer.txt", "content": contents[number - 1]}}]
    )


def test_the_best_attempt_wins_and_the_project_is_untouched(agent_project: Path, make_app) -> None:  # noqa: F811
    app = make_app(agent_project)
    roots: list[Path] = []

    class Tracking(project_copies):
        def __call__(self, attempt: int):  # type: ignore[no-untyped-def]
            env = super().__call__(attempt)
            roots.append(env.root)
            return env

    copies = Tracking(app, approvals=RecordingUI())
    runner = BestOfN(copies, planners(["wrong\n", "ok\n", "never run\n"]), attempts=3)
    task = AgentTask("answer", surface="none", success={"file": {"path": "answer.txt", "contains": "ok"}})
    result = runner.run(task)
    assert [a.score for a in result.attempts] == [0.0, 1.0] and result.stopped == "perfect"  # the third never ran
    assert result.best is not None and result.best.number == 2
    assert "+ok" in result.diff and "b/answer.txt" in result.diff
    assert not (agent_project / "answer.txt").exists()  # applying the winner is the person's decision
    assert roots and all(not r.exists() for r in roots)  # every attempt's copy was cleaned up


def test_budgets_and_isolation(agent_project: Path, make_app) -> None:  # noqa: F811
    app = make_app(agent_project)
    copies = project_copies(app, approvals=RecordingUI())
    capped = BestOfN(copies, planners(["a\n", "b\n", "c\n"]), attempts=3, seconds=0.0)
    assert capped.run(AgentTask("x", surface="none")).stopped == "time budget"
    no_success_check = BestOfN(copies, planners(["a\n", "b\n"]), attempts=2).run(AgentTask("x", surface="none"))
    assert [a.score for a in no_success_check.attempts] == [0.5, 0.5] and no_success_check.stopped == "attempts"

    class SharedDesktop:
        isolated = False

    with pytest.raises(UsageError, match="isolated"):
        BestOfN(SharedDesktop(), planners(["a\n"]))  # type: ignore[arg-type]
    with pytest.raises(UsageError, match="between 1 and 10"):
        BestOfN(copies, planners(["a\n"]), attempts=50)


def test_secrets_are_not_copied_into_attempts(agent_project: Path, make_app) -> None:  # noqa: F811
    (agent_project / ".env").write_text("TOKEN=abc\n")
    app = make_app(agent_project)
    env = project_copies(app)(1)
    try:
        assert not (env.root / ".env").exists() and (env.root / "pyproject.toml").exists()
    finally:
        env.cleanup()


def test_best_of_on_the_cli_needs_a_model_and_no_screen(cli, agent_project: Path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    out = cli("agent", "loop", "fix it", "--best-of", "2", cwd=agent_project)
    assert out.code != 0 and "--best-of needs --model and --surface none" in (out.stderr + out.stdout)
