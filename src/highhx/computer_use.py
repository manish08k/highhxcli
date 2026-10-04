"""The computer-use runtime as the CLI and the TUI use it: one place that builds the executor,
the planner, the stores and the live view, then runs or resumes a task.

Nothing here adds a way to act. Every task runs on an :class:`~highhx.actions.executor.ActionExecutor`
(risk, policy, approval on this terminal, verification, audit), and the live dashboard only
watches events.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from highhx.core.errors import UsageError

if TYPE_CHECKING:
    from highhx.actions.executor import ActionExecutor
    from highhx.agent.loop import AgentPlanner, AgentTask, LoopResult
    from highhx.commands import App
    from highhx.ui.live import DashboardState, LiveDashboard


def header(app: App) -> dict[str, str]:
    """Branding facts for the TUI and dashboard (no network: the cached account only)."""
    info = {"project": app.root.name, "branch": "", "plan": "Free", "account": "", "connection": "local"}
    with contextlib.suppress(Exception):
        info["branch"] = app.git_repo.current_branch() or ""
    with contextlib.suppress(Exception):
        creds = app.cloud.credentials
        if creds.signed_in:
            from highhx.cloud.account import Account

            account = Account.from_dict(creds.account, cached=True) if creds.account else None
            info["account"] = account.email if account else "signed in"
            info["plan"] = "Pro" if account is not None and account.is_pro else "Free"
    with contextlib.suppress(Exception):
        from highhx.connections import computer_target

        info["connection"] = f"computer: {computer_target(app)}"
    return info


def dashboard_state(app: App) -> DashboardState:
    from highhx.ui.live import DashboardState

    facts = header(app)
    return DashboardState(project=facts["project"], branch=facts["branch"], plan_name=facts["plan"], account=facts["account"], connection=facts["connection"])


@dataclass
class Session:
    """An executor (and, with ``live``, the dashboard watching it) for one command."""

    app: App
    executor: ActionExecutor
    dashboard: LiveDashboard | None = None
    closers: list[Any] = field(default_factory=list)

    def close(self) -> None:
        for close in reversed(self.closers):
            with contextlib.suppress(Exception):
                close()
        self.closers.clear()


@contextlib.contextmanager
def session(app: App, *, agent: bool = False, live: bool = False, source: str = "computer-use") -> Iterator[Session]:
    """The executor for a task: the user's own actions (USER) or a model's proposals (AGENT,
    the stricter rules). With ``live``, the dashboard runs while the task does and every approval
    prompt pauses it first."""
    from highhx.actions.catalog import catalog_for
    from highhx.actions.executor import ActionExecutor
    from highhx.agent.ui import TerminalUI
    from highhx.observability.stream import EventRecorder
    from highhx.safety.actions import Actor
    from highhx.safety.audit import AuditLog
    from highhx.safety.gate import ActionGate, ApprovalMode
    from highhx.ui.live import LiveDashboard, PausingPrompter

    prompter: Any = TerminalUI(app.output.err_console, app.output.symbols, interactive=app.options.is_interactive())
    dashboard = None
    closers: list[Any] = []
    if live:
        recorder = EventRecorder.attach(app.ctx.events, redactor=app.redactor)
        dashboard = LiveDashboard(app.output.console, recorder, dashboard_state(app))
        prompter = PausingPrompter(prompter, dashboard)
        closers.append(recorder.close)
    gate = ActionGate(
        app.engine,
        prompter,
        source=source,
        mode=ApprovalMode.ASK,
        assume_yes=app.options.yes,
        audit=AuditLog(app.db, app.redactor) if app.db is not None else None,
    )
    executor = ActionExecutor(app, gate, actor=Actor.AGENT if agent else Actor.USER, catalog=catalog_for(app))
    closers.insert(0, executor.close)
    handle = Session(app, executor, dashboard, closers)
    try:
        if dashboard is not None:
            dashboard.start()
        yield handle
    finally:
        if dashboard is not None:
            dashboard.stop()
        handle.close()


def load_plan(path: Path) -> list[dict[str, Any]]:
    """Steps from a YAML/JSON file: a list of steps, or ``{steps: [...]}``."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.suffix in (".yaml", ".yml") else json.loads(path.read_text(encoding="utf-8"))
    steps = data.get("steps") if isinstance(data, dict) else data
    if not isinstance(steps, list) or not all(isinstance(s, dict) and s.get("action") for s in steps):
        raise UsageError(f"{path.name} is not a plan: expected a list of steps with an action each.")
    return steps


def planner_for(app: App, executor: ActionExecutor, goal: str, *, plan: Path | None, model: bool, remote_model: bool) -> AgentPlanner:
    """Scripted (``--plan``), a model (``--model``: HighhX Pro or a local model), or HighhX
    Free's deterministic resolver."""
    from highhx.agent.loop import ModelPlanner, ResolverPlanner, ScriptedPlanner

    if plan is not None:
        return ScriptedPlanner(load_plan(plan))
    if model:
        from highhx.models.registry import language_model

        return ModelPlanner(language_model(app, app.ctx.cancel, remote_ok=remote_model), executor.catalog)
    return ResolverPlanner(app, goal, executor=executor)


def stores(app: App) -> tuple[Any, Any]:
    from highhx.observability.tasktrace import TraceStore
    from highhx.trajectories import TrajectoryStore

    return TrajectoryStore.for_app(app), TraceStore.for_app(app)


def run_task(executor: ActionExecutor, planner: AgentPlanner, task: AgentTask, *, specialists: bool = False) -> LoopResult:
    from highhx.agent.loop import AgentLoop, default_router

    trajectories, traces = stores(executor.app)
    loop = AgentLoop(executor, planner, store=trajectories, traces=traces, router=default_router(trajectories))
    return loop.run(task)


def resume_task(executor: ActionExecutor, task_id: str, *, planner: AgentPlanner | None = None) -> LoopResult:
    from highhx.agent.loop import default_router, resume

    trajectories, traces = stores(executor.app)
    return resume(executor, trajectories, task_id, planner=planner, traces=traces, router=default_router())


def replay_trajectory(executor: ActionExecutor, task_id: str) -> LoopResult:
    """Run a past task again from its semantic steps (targets re-grounded, never coordinates)."""
    from highhx.agent.loop import AgentLoop, AgentTask, ScriptedPlanner
    from highhx.trajectories import replay_steps

    trajectories, traces = stores(executor.app)
    past = trajectories.load(task_id)
    steps = replay_steps(past)
    if any(s["parameters"].get("__redacted__") for s in steps):
        raise UsageError("This task typed into a secret field; that text was not stored, so it cannot be replayed.")
    loop = AgentLoop(executor, ScriptedPlanner(steps), store=trajectories, traces=traces, agent="replay", memory=False)
    return loop.run(AgentTask(f"replay: {past.task}", surface=past.surface or "auto"))
