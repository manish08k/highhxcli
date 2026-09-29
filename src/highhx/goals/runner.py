"""Putting a goal run together for the CLI: the Task IR, the planners and the log.

    request ──► deterministic resolver ──► Task IR            (HighhX Free, no AI)
            └─► (Pro) model planner writes the Task IR       (requests nobody programmed)
    Task IR ──► GoalLoop(ScriptedPlanner, replanner=ModelPlanner on Pro) ──► TaskState

Free never loads a model: a request the resolver does not understand, or an IR without steps
(it must be discovered from the page), needs HighhX Pro and says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from highhx.core.errors import AccountError, CloudError, PlanRequiredError, UsageError, ValidationError
from highhx.goals import understand
from highhx.goals.ir import TaskIR, loads_json, parse_task
from highhx.goals.planner import ModelPlanner

if TYPE_CHECKING:
    from highhx.commands import App
    from highhx.execution.cancellation import CancellationToken

PRO_HINT = "HighhX Free runs requests the deterministic resolver understands; `highhx login` for HighhX Pro."


def load_ir(path: Path) -> TaskIR:
    """Task IR from a JSON or YAML file."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            data: Any = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValidationError(f"{path} is not valid YAML.", details=[str(exc)]) from None
    else:
        data = loads_json(text)
    return parse_task(data)


def model_planner(app: App, cancel: CancellationToken | None) -> ModelPlanner | None:
    """The Pro planner, when this account has AI computer use and the platform is reachable."""
    from highhx.agent.bootstrap import build_provider, resolve_settings
    from highhx.cloud.plans import AGENT_COMPUTER_USE

    cloud = app.cloud
    if not cloud.signed_in:
        return None
    try:
        account = cloud.require(AGENT_COMPUTER_USE, what="AI computer use")
    except (AccountError, CloudError):
        return None
    if account.cached:
        return None  # a cached account never starts the model (as for the agent)
    settings = resolve_settings(app, account, {})
    return ModelPlanner(build_provider(settings, cloud, account), model=settings.model, cancel=cancel)


@dataclass
class Prepared:
    task: TaskIR
    source: str
    """"file", "deterministic" or "model" — where the IR came from."""
    replanner: ModelPlanner | None


def prepare(app: App, request: str, ir_file: Path | None, cancel: CancellationToken | None) -> Prepared:
    from highhx.actions.resolver import ResolverContext

    planner = model_planner(app, cancel)
    if ir_file is not None:
        task, source = load_ir(ir_file), "file"
    else:
        if not request.strip():
            raise UsageError("Describe the task, or pass --ir FILE.")
        try:
            found = understand.deterministic(request, ResolverContext.from_app(app))
        except understand.NotBrowserTask as exc:
            raise UsageError(
                f"That request runs {', '.join(exc.actions)} — not browser work.",
                hint=f'Run it as a normal request: highhx "{request}"',
            ) from None
        if found is not None:
            task, source = found, "deterministic"
        elif planner is not None:
            task, source = planner.understand(request), "model"
        else:
            raise PlanRequiredError(
                "HighhX Free has no deterministic plan for this request; understanding new tasks is HighhX Pro.",
                hint=PRO_HINT,
            )
    if task.dynamic and planner is None:
        raise PlanRequiredError(
            "This task has no steps: discovering them from the page needs the HighhX Pro planner.", hint=PRO_HINT
        )
    return Prepared(task, source, planner if task.allow_replanning or task.dynamic else None)


def with_limits(task: TaskIR, *, max_steps: int | None, timeout: float | None) -> TaskIR:
    import dataclasses

    changes: dict[str, Any] = {}
    if max_steps is not None:
        changes["max_steps"] = max_steps
    if timeout is not None:
        changes["timeout"] = timeout
    if not changes:
        return task
    return dataclasses.replace(task, constraints=dataclasses.replace(task.constraints, **changes))
