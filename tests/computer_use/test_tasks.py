"""Computer-use tasks, end to end: instruction → HighhX agent (a recorded trajectory stands in
for the model) → approvals → action executor → HighhX Computer API → bridge → a simulated
desktop → evaluator. Deterministic: no model, no real input — the harness for measuring the
runtime on tasks, not a production component.

    tasks/<id>.yaml          instruction, success criteria (and ``decline`` for safety tasks)
    tasks/_desktop.yaml      the starting desktop
    trajectories/<id>.yaml   the agent's tool calls
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from highhx.automation.engine.bridge import AutomationBridge
from highhx.computer.driver import HighhXDriver
from highhx.computer.session import ComputerSession
from tests.computer_use.environment import SimulatedDesktop
from tests.computer_use.evaluators import evaluate
from tests.unit.agent.conftest import RecordingUI, reply

HERE = Path(__file__).parent
TASKS = sorted(p for p in (HERE / "tasks").glob("*.yaml") if not p.name.startswith("_"))


def load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("task_file", TASKS, ids=[p.stem for p in TASKS])
def test_task(task_file: Path, agent_project: Path, make_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    task = load(task_file)
    trajectory = load(HERE / "trajectories" / task_file.name)
    env = SimulatedDesktop(load(HERE / "tasks" / "_desktop.yaml"))
    driver = HighhXDriver(AutomationBridge(env))
    monkeypatch.setattr(ComputerSession, "driver", lambda self: driver)
    steps = [reply("", [(s["tool"], s["args"])]) for s in trajectory["steps"]] + [reply("Done.")]
    ui = RecordingUI(default_action_answer=not task.get("decline", False))
    session, _provider, _ = make_session(agent_project, steps, ui=ui)
    assert session.run_turn(task["instruction"]).stopped == "completed"
    result = evaluate(task["id"], env, task["success"])
    assert result.passed, f"{task['id']}: score {result.score:.2f} — {result.checks}"


def test_every_task_has_a_trajectory_and_known_criteria() -> None:
    from tests.computer_use.evaluators import CHECKS

    assert TASKS
    for task_file in TASKS:
        task = load(task_file)
        assert task["id"] == task_file.stem and (HERE / "trajectories" / task_file.name).is_file()
        assert all(next(iter(c)) in CHECKS for c in task["success"]), task["id"]


def test_a_trajectory_that_skips_a_step_fails_its_evaluation(
    agent_project: Path, make_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evaluators are not vacuous: leaving out "save" leaves the task undone."""
    task = load(HERE / "tasks" / "fill_and_save.yaml")
    trajectory = load(HERE / "trajectories" / "fill_and_save.yaml")["steps"][:-1]
    env = SimulatedDesktop(load(HERE / "tasks" / "_desktop.yaml"))
    driver = HighhXDriver(AutomationBridge(env))
    monkeypatch.setattr(ComputerSession, "driver", lambda self: driver)
    session, _provider, _ = make_session(
        agent_project, [*(reply("", [(s["tool"], s["args"])]) for s in trajectory), reply("Done.")]
    )
    session.run_turn(task["instruction"])
    result = evaluate(task["id"], env, task["success"])
    assert not result.passed and result.score == 0.5
    assert [ok for _, ok, _ in result.checks] == [True, False]
