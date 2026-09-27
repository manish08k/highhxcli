"""The JSON action plan (version 1): what the deterministic resolver decided, as data.

    {
      "version": "1",
      "request": "open Gmail and search internship",
      "intent": "workflow",
      "target": "gmail",
      "risk": "safe",               ← safe / controlled / high
      "risk_level": "low",          ← the executor's five levels: safe, low, medium, high, critical
      "executor": "browser",
      "steps": [
        {"id": "step_1", "action": "open", "target": "gmail", "catalog_action": "browser.open",
         "params": {"url": "https://mail.google.com/"}, "risk": "safe", "risk_level": "low",
         "executor": "browser", "verification": {"type": "page_open"}, "description": "open Gmail"},
        …
      ],
      "verification": {"type": "search_results"}
    }

A plan is deterministic (the same request and project give the same plan), serialisable,
versioned and independent of the UI. It is *not* a capability: executing a plan runs each
step through the action executor, which classifies it again and asks for approval by the
same policy as any other action. :func:`validate_plan` rejects a plan (e.g. one loaded from a
file) that names an unknown action, invalid parameters or a mismatched primitive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from highhx.actions.catalog import Catalog

PLAN_VERSION = "1"
RISK_CLASSES = ("safe", "controlled", "high")
RISK_LEVELS = ("safe", "low", "medium", "high", "critical")
EXECUTORS = ("browser", "desktop", "filesystem", "shell", "git", "project", "system", "workflow")


class PlanError(ValueError):
    """A plan that does not satisfy the schema."""


@dataclass(frozen=True)
class PlanStep:
    id: str
    action: str
    """The primitive: open, search, play, launch, focus, click, type, press, hotkey, scroll, list,
    create, run, test, status, diff …"""
    catalog_action: str
    """The executor action that performs it (``browser.search``)."""
    target: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    risk: str = "safe"
    risk_level: str = "safe"
    executor: str = "system"
    verification: str = "none"
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "target": self.target,
            "catalog_action": self.catalog_action,
            "params": dict(self.params),
            "risk": self.risk,
            "risk_level": self.risk_level,
            "executor": self.executor,
            "verification": {"type": self.verification},
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanStep:
        verification = data.get("verification") or {}
        return cls(
            id=str(data["id"]),
            action=str(data["action"]),
            catalog_action=str(data["catalog_action"]),
            target=str(data.get("target") or ""),
            params=dict(data.get("params") or {}),
            risk=str(data.get("risk") or "safe"),
            risk_level=str(data.get("risk_level") or "safe"),
            executor=str(data.get("executor") or "system"),
            verification=str(verification.get("type") if isinstance(verification, dict) else verification or "none"),
            description=str(data.get("description") or ""),
        )


@dataclass(frozen=True)
class ActionPlan:
    request: str
    intent: str
    target: str
    risk: str
    risk_level: str
    executor: str
    steps: tuple[PlanStep, ...]
    verification: str
    version: str = PLAN_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "request": self.request,
            "intent": self.intent,
            "target": self.target,
            "risk": self.risk,
            "risk_level": self.risk_level,
            "executor": self.executor,
            "steps": [s.to_dict() for s in self.steps],
            "verification": {"type": self.verification},
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionPlan:
        try:
            verification = data.get("verification") or {}
            return cls(
                request=str(data.get("request") or ""),
                intent=str(data["intent"]),
                target=str(data.get("target") or ""),
                risk=str(data["risk"]),
                risk_level=str(data.get("risk_level") or "safe"),
                executor=str(data.get("executor") or "system"),
                steps=tuple(PlanStep.from_dict(s) for s in data["steps"]),
                verification=str(verification.get("type") if isinstance(verification, dict) else verification),
                version=str(data["version"]),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise PlanError(f"not an action plan: {exc}") from None

    @classmethod
    def from_json(cls, text: str) -> ActionPlan:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise PlanError(f"not JSON: {exc}") from None
        if not isinstance(data, dict):
            raise PlanError("an action plan is a JSON object")
        return cls.from_dict(data)


def validate_plan(plan: ActionPlan, catalog: Catalog) -> list[str]:
    """Problems that make ``plan`` unusable (empty: valid). Checks the schema, that every step
    names a catalog action with valid parameters, and that its primitive and executor match
    what that action is."""
    from highhx.plans.planner import describe_action

    problems: list[str] = []
    if plan.version != PLAN_VERSION:
        problems.append(f"version {plan.version!r} is not supported (expected {PLAN_VERSION!r})")
    if plan.risk not in RISK_CLASSES:
        problems.append(f"risk {plan.risk!r} is not one of {', '.join(RISK_CLASSES)}")
    if plan.risk_level not in RISK_LEVELS:
        problems.append(f"risk_level {plan.risk_level!r} is not one of {', '.join(RISK_LEVELS)}")
    if not plan.steps:
        problems.append("a plan needs at least one step")
    seen: set[str] = set()
    for step in plan.steps:
        where = f"step {step.id!r}"
        if step.id in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(step.id)
        spec = catalog.get(step.catalog_action)
        if spec is None:
            problems.append(f"{where}: unknown action {step.catalog_action!r}")
            continue
        problems += [f"{where}: {p}" for p in spec.validate(step.params)]
        expected = describe_action(step.catalog_action, step.params)
        if step.action != expected.primitive:
            problems.append(f"{where}: {step.catalog_action} is {expected.primitive!r}, not {step.action!r}")
        if step.executor != expected.executor:
            problems.append(f"{where}: {step.catalog_action} runs on {expected.executor!r}, not {step.executor!r}")
        if step.risk not in RISK_CLASSES or step.risk_level not in RISK_LEVELS:
            problems.append(f"{where}: invalid risk")
    return problems


PLAN_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://highhx.dev/schemas/action-plan-1.json",
    "title": "HighhX action plan",
    "type": "object",
    "required": ["version", "intent", "risk", "steps", "verification"],
    "additionalProperties": False,
    "properties": {
        "version": {"const": PLAN_VERSION},
        "request": {"type": "string"},
        "intent": {"type": "string"},
        "target": {"type": "string"},
        "risk": {"enum": list(RISK_CLASSES)},
        "risk_level": {"enum": list(RISK_LEVELS)},
        "executor": {"enum": [*EXECUTORS, "mixed"]},
        "verification": {"type": "object", "required": ["type"], "properties": {"type": {"type": "string"}}},
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "action", "catalog_action", "params", "risk", "executor", "verification"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "action": {"type": "string"},
                    "target": {"type": "string"},
                    "catalog_action": {"type": "string", "pattern": r"^[a-z]+\.[a-z_]+$"},
                    "params": {"type": "object"},
                    "risk": {"enum": list(RISK_CLASSES)},
                    "risk_level": {"enum": list(RISK_LEVELS)},
                    "executor": {"enum": list(EXECUTORS)},
                    "verification": {
                        "type": "object",
                        "required": ["type"],
                        "properties": {"type": {"type": "string"}},
                    },
                    "description": {"type": "string"},
                },
            },
        },
    },
}
