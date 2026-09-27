"""Fixtures for the action engine: an app on a copy of the Python fixture project, a recording
prompter (answers queued, default approve) and executors for the user and for the agent."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.executor import ActionExecutor
from highhx.commands import App
from highhx.safety.actions import Actor
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.unit.agent.conftest import RecordingUI, agent_project, make_app, make_session  # noqa: F401


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def git_init(root: Path) -> None:
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")


@pytest.fixture
def repo(agent_project: Path) -> Path:  # noqa: F811
    git_init(agent_project)
    return agent_project


@pytest.fixture
def executor_for(make_app: Callable[..., App]) -> Callable[..., tuple[ActionExecutor, RecordingUI]]:  # noqa: F811
    def _make(
        root: Path,
        *,
        actor: Actor = Actor.USER,
        mode: ApprovalMode = ApprovalMode.ASK,
        ui: RecordingUI | None = None,
        catalog: Any = None,
        **options: Any,
    ) -> tuple[ActionExecutor, RecordingUI]:
        app = make_app(root, **options)
        ui = ui or RecordingUI(interactive=app.options.is_interactive())
        from highhx.safety.audit import AuditLog

        gate = ActionGate(
            app.engine,
            ui,
            source="test",
            mode=mode,
            assume_yes=app.options.yes,
            audit=AuditLog(app.db, app.redactor) if app.db is not None else None,
        )
        kwargs: dict[str, Any] = {"actor": actor, "sleep": lambda _s: None}
        if catalog is not None:
            kwargs["catalog"] = catalog
        return ActionExecutor(app, gate, **kwargs), ui

    return _make
