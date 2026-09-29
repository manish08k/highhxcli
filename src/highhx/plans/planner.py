"""Build an :class:`ActionPlan` from resolved steps.

For every catalog action the planner knows the *primitive* it performs (open, search, play …),
the *executor* that carries it out (the HighhX browser, the desktop automation bridge, the
file system, the shell, git, the project toolchain) and the *verification* strategy that checks
it. Actions without an entry here are described by their category, so every catalog action
can appear in a plan.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from highhx.actions.policy import Risk
from highhx.decision.risk import risk_class, worst
from highhx.plans.schema import ActionPlan, PlanStep

if TYPE_CHECKING:
    from highhx.actions.catalog import Catalog
    from highhx.actions.resolver import Step


@dataclass(frozen=True)
class ActionInfo:
    primitive: str
    executor: str
    verification: str


# catalog action → (primitive, executor, verification)
ACTIONS: dict[str, ActionInfo] = {
    "browser.open": ActionInfo("open", "browser", "page_open"),
    "browser.search": ActionInfo("search", "browser", "search_results"),
    "browser.play": ActionInfo("play", "browser", "media_playing"),
    "browser.click": ActionInfo("click", "browser", "element_clicked"),
    "browser.fill": ActionInfo("type", "browser", "element_clicked"),
    "browser.press": ActionInfo("press", "browser", "none"),
    "browser.find": ActionInfo("find", "browser", "none"),
    "browser.observe": ActionInfo("inspect", "browser", "none"),
    "computer.launch": ActionInfo("launch", "desktop", "app_running"),
    "computer.focus": ActionInfo("focus", "desktop", "app_frontmost"),
    "computer.type": ActionInfo("type", "desktop", "keys_sent"),
    "computer.press": ActionInfo("press", "desktop", "keys_sent"),
    "computer.hotkey": ActionInfo("hotkey", "desktop", "keys_sent"),
    "computer.click": ActionInfo("click", "desktop", "element_clicked"),
    "computer.scroll": ActionInfo("scroll", "desktop", "none"),
    "filesystem.list": ActionInfo("list", "filesystem", "listing"),
    "filesystem.open": ActionInfo("open", "desktop", "opened_by_os"),
    "filesystem.create": ActionInfo("create", "filesystem", "file_exists"),
    "filesystem.write": ActionInfo("write", "filesystem", "file_exists"),
    "filesystem.read": ActionInfo("read", "filesystem", "command_captured"),
    "filesystem.find": ActionInfo("find", "filesystem", "files_found"),
    "shell.run": ActionInfo("run", "shell", "process_exit"),
    "project.test": ActionInfo("test", "project", "process_exit"),
    "project.build": ActionInfo("build", "project", "process_exit"),
    "project.check": ActionInfo("check", "project", "process_exit"),
    "project.fix": ActionInfo("fix", "project", "process_exit"),
}
_BY_CATEGORY = {
    "git": ("git", "command_captured"),
    "project": ("project", "process_exit"),
    "package": ("project", "process_exit"),
    "service": ("project", "process_exit"),
    "deployment": ("project", "process_exit"),
    "security": ("project", "command_captured"),
    "workflow": ("workflow", "process_exit"),
    "database": ("project", "process_exit"),
    "filesystem": ("filesystem", "command_captured"),
    "browser": ("browser", "none"),
    "computer": ("desktop", "none"),
    "shell": ("shell", "process_exit"),
}


def describe_action(action: str, params: dict[str, Any] | None = None) -> ActionInfo:
    params = params or {}
    info = ACTIONS.get(action)
    if info is None:
        category, _, verb = action.partition(".")
        executor, verification = _BY_CATEGORY.get(category, ("system", "command_captured"))
        info = ActionInfo(verb, executor, verification)
    if action in ("browser.open", "browser.search") and params.get("app"):
        # a browser HighhX cannot drive: the OS opens the URL there
        return ActionInfo(info.primitive, "desktop", info.verification)
    if action == "computer.scroll" and params.get("source", "browser") == "browser":
        return ActionInfo(info.primitive, "browser", info.verification)
    return info


RiskOf = Callable[[str, dict[str, Any]], Risk]
"""The effective risk of an action with these inputs (the executor's classification)."""


def catalog_risk(catalog: Catalog) -> RiskOf:
    """Risk from the catalog alone (the action's floor, including ``risk_for``) — for previews
    without an executor. Execution may classify higher (the command classifier), never lower."""

    def risk_of(action: str, params: dict[str, Any]) -> Risk:
        spec = catalog.get(action)
        if spec is None:
            return Risk.HIGH
        floor = spec.risk
        if spec.risk_for is not None:
            floor = max(floor, spec.risk_for(params))
        return floor

    return risk_of


def build_plan(request: str, steps: list[Step], catalog: Catalog, risk_of: RiskOf) -> ActionPlan:
    """The plan for resolved ``steps`` (in order)."""
    planned: list[PlanStep] = []
    for index, step in enumerate(steps, start=1):
        spec = catalog.get(step.action)
        info = describe_action(step.action, step.inputs)
        level = risk_of(step.action, dict(step.inputs))
        planned.append(
            PlanStep(
                id=f"step_{index}",
                action=info.primitive,
                catalog_action=step.action,
                target=step.target or _default_target(step),
                params=dict(step.inputs),
                risk=risk_class(level, spec.kind if spec is not None else None),
                risk_level=level.label,
                executor=info.executor,
                verification=info.verification,
                description=step.description or step.action,
            )
        )
    levels = [Risk.parse(s.risk_level) for s in planned]
    executors = {s.executor for s in planned}
    first = planned[0]
    return ActionPlan(
        request=request,
        intent=first.action if len(planned) == 1 else "workflow",
        target=next((s.target for s in planned if s.target), ""),
        risk=worst([s.risk for s in planned]),
        risk_level=max(levels).label,
        executor=executors.pop() if len(executors) == 1 else "mixed",
        steps=tuple(planned),
        verification=planned[-1].verification,
    )


def _default_target(step: Step) -> str:
    for key in ("site", "app", "name", "path", "environment", "ref", "service"):
        value = step.inputs.get(key)
        if value:
            return str(value).lower() if key in ("site", "app") else str(value)
    category = step.action.split(".", 1)[0]
    return "project" if category in ("project", "git", "package", "service", "security", "workflow") else ""
