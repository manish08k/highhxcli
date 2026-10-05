"""Trajectories record where and how they ran (reproducibility), and keep it through save/load."""

from __future__ import annotations

import platform
import re
from pathlib import Path

from highhx import __version__
from highhx.actions.executor import ActionExecutor
from highhx.agent.loop import AgentLoop, AgentTask, ScriptedPlanner
from highhx.safety.actions import Actor
from highhx.safety.gate import ActionGate, ApprovalMode
from highhx.trajectories import TrajectoryStore
from tests.unit.actions.conftest import repo  # noqa: F401
from tests.unit.agent.conftest import RecordingUI, agent_project, make_app  # noqa: F401


def test_a_trajectory_records_its_environment(repo: Path, make_app, tmp_path: Path) -> None:  # noqa: F811
    app = make_app(repo)
    executor = ActionExecutor(
        app, ActionGate(app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK), actor=Actor.USER
    )
    store = TrajectoryStore(tmp_path / "t")
    steps = [{"action": "filesystem.write", "parameters": {"path": "a.txt", "content": "x\n"}}]
    result = AgentLoop(executor, ScriptedPlanner(steps), store=store, sleep=lambda _s: None).run(
        AgentTask("write", surface="none")
    )
    env = store.load(result.trajectory.id).environment
    assert env["highhx"] == __version__ and env["python"] == platform.python_version() and env["planner"] == "scripted"
    assert re.fullmatch(r"[0-9a-f]{40}", env["project_commit"])
    assert "model" not in env  # no model planned this task
