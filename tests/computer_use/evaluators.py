"""Evaluators: a task's success criteria, checked against the environment's final state.

Each criterion is data (``{"field_value": {"app": "Notes", "name": "Title", "equals": "Plan"}}``)
so tasks stay declarative; the score is the fraction of criteria that hold, and a task passes
only when all do."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tests.computer_use.environment import SimulatedDesktop


@dataclass
class Evaluation:
    task: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def score(self) -> float:
        return sum(ok for _, ok, _ in self.checks) / len(self.checks) if self.checks else 0.0

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)


def _field_value(env: SimulatedDesktop, spec: dict[str, Any]) -> tuple[bool, str]:
    value = env.element(spec["name"], spec.get("app")).get("value", "")
    return value == spec["equals"], f"{spec['name']} = {value!r}"


def _pressed(env: SimulatedDesktop, spec: dict[str, Any]) -> tuple[bool, str]:
    presses = env.element(spec["name"], spec.get("app")).get("presses", 0)
    return presses == spec["times"], f"{spec['name']} pressed {presses} time(s)"


def _checked(env: SimulatedDesktop, spec: dict[str, Any]) -> tuple[bool, str]:
    checked = bool(env.element(spec["name"], spec.get("app")).get("checked"))
    return checked == spec["is"], f"{spec['name']} checked={checked}"


def _saved(env: SimulatedDesktop, spec: dict[str, Any]) -> tuple[bool, str]:
    saved = bool(env.apps[spec["app"]].get("saved"))
    return saved == spec.get("is", True), f"{spec['app']} saved={saved}"


def _running(env: SimulatedDesktop, spec: dict[str, Any]) -> tuple[bool, str]:
    running = bool(env.apps[spec["app"]].get("running", True))
    return running == spec["is"], f"{spec['app']} running={running}"


def _clipboard(env: SimulatedDesktop, spec: dict[str, Any]) -> tuple[bool, str]:
    return env.clipboard == spec["equals"], f"clipboard = {env.clipboard!r}"


def _window(env: SimulatedDesktop, spec: dict[str, Any]) -> tuple[bool, str]:
    window = env.apps[spec["app"]]["window"]
    frame = [window[k] for k in ("x", "y", "width", "height")]
    return frame == spec["frame"], f"{spec['app']} frame {frame}"


def _never(env: SimulatedDesktop, spec: dict[str, Any]) -> tuple[bool, str]:
    sent = [op for op, _ in env.log if op in spec["ops"]]
    return not sent, f"sent {sent}" if sent else "never sent"


CHECKS: dict[str, Callable[[SimulatedDesktop, dict[str, Any]], tuple[bool, str]]] = {
    "field_value": _field_value,
    "pressed": _pressed,
    "checked": _checked,
    "saved": _saved,
    "running": _running,
    "clipboard": _clipboard,
    "window": _window,
    "never": _never,
}


def evaluate(task: str, env: SimulatedDesktop, criteria: list[dict[str, Any]]) -> Evaluation:
    result = Evaluation(task)
    for criterion in criteria:
        ((kind, spec),) = criterion.items()
        ok, detail = CHECKS[kind](env, spec)
        result.checks.append((kind, ok, detail))
    return result
