"""Application skills: reusable, checked knowledge about using one application or site.

A skill is a YAML file (``highhx/skills/builtin/*.yaml``, or ``.highhx/skills/*.yaml`` in a
project) with: name, version, description, where it applies (surfaces, hosts, apps), the
capabilities it needs, the catalog actions it uses, permission notes, examples (runnable steps),
verification checks and failure modes with their recovery.

Skills grant nothing: their examples run through the agent loop and executor like any plan, and
the planner sees matching skills only as data (bounded notes). ``check()`` validates a skill
against the real catalog — an unknown action or step makes it invalid — and against
``highhx capabilities``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BUILTIN = Path(__file__).parent / "builtin"
REQUIRED = ("name", "version", "description", "actions", "examples")


@dataclass(frozen=True)
class Skill:
    name: str
    version: int
    description: str
    applies_to: dict[str, list[str]]
    requires: list[str]
    actions: list[str]
    permissions: list[str]
    examples: list[dict[str, Any]]
    verification: list[dict[str, Any]]
    failure_modes: list[dict[str, str]]
    source: str = ""
    problems: list[str] = field(default_factory=list)

    def matches(self, *, surface: str = "", host: str = "", app: str = "") -> bool:
        where = self.applies_to
        if surface and where.get("surfaces") and surface not in where["surfaces"]:
            return False
        if host and where.get("hosts") and not any(host == h or host.endswith("." + h) for h in where["hosts"]):
            return False
        return not (app and where.get("apps") and app.lower() not in (a.lower() for a in where["apps"]))

    def note(self) -> str:
        """One bounded line for a planner: what the skill knows (data, not instructions)."""
        recoveries = "; ".join(f"{f['symptom']} → {f['recovery']}" for f in self.failure_modes[:3])
        return (
            f"skill {self.name}: {self.description} Uses {', '.join(self.actions[:6])}. Known problems: {recoveries}"[
                :300
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def parse(data: Any, source: str = "") -> Skill:
    problems = []
    if not isinstance(data, dict):
        data, problems = {}, ["a skill is a mapping"]
    problems += [f"missing {key}" for key in REQUIRED if key not in data]
    return Skill(
        name=str(data.get("name") or ""),
        version=int(data.get("version") or 0),
        description=str(data.get("description") or ""),
        applies_to={k: [str(v) for v in vs] for k, vs in (data.get("applies_to") or {}).items()},
        requires=[str(r) for r in data.get("requires") or []],
        actions=[str(a) for a in data.get("actions") or []],
        permissions=[str(p) for p in data.get("permissions") or []],
        examples=list(data.get("examples") or []),
        verification=list(data.get("verification") or []),
        failure_modes=[dict(f) for f in data.get("failure_modes") or []],
        source=source,
        problems=problems,
    )


def load(project_root: Path | None = None) -> list[Skill]:
    """Built-in skills, then the project's (a project skill with the same name replaces a built-in)."""
    found: dict[str, Skill] = {}
    folders = [BUILTIN] + ([project_root / ".highhx" / "skills"] if project_root is not None else [])
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.yaml")):
            try:
                skill = parse(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))
            except yaml.YAMLError as exc:
                skill = parse({}, str(path))
                skill.problems.append(f"not valid YAML: {exc}")
            found[skill.name or path.stem] = skill
    return list(found.values())


def check(skill: Skill, catalog: Any, available: set[str] | None = None) -> list[str]:
    """Problems: missing fields, unknown actions, example steps that are not valid plan steps or use
    actions the skill does not declare, and (with ``available``) required capabilities that are missing."""
    from highhx.agent.loop.model import VERBS

    problems = list(skill.problems)
    known = set(catalog.names())
    for action in skill.actions:
        if action not in known:
            problems.append(f"unknown action {action!r}")
    for index, example in enumerate(skill.examples):
        if not isinstance(example, dict) or not example.get("task") or not isinstance(example.get("steps"), list):
            problems.append(f"example {index + 1} needs a task and steps")
            continue
        for step in example["steps"]:
            action = str((step or {}).get("action") or "")
            if action in VERBS:
                continue
            if action not in known:
                problems.append(f"example {index + 1}: unknown step action {action!r}")
            elif action not in skill.actions:
                problems.append(f"example {index + 1}: {action} is not among the skill's actions")
    for fm in skill.failure_modes:
        if not fm.get("symptom") or not fm.get("recovery"):
            problems.append("each failure mode needs a symptom and a recovery")
    if available is not None:
        for need in skill.requires:
            if need not in available:
                problems.append(f"needs {need}, which is not available here")
    return problems


def relevant(
    skills: list[Skill], *, surface: str, goal: str, host: str = "", app: str = "", limit: int = 2
) -> list[Skill]:
    """Skills for this surface whose name, hosts or apps the goal mentions (or that match host/app)."""
    lowered = goal.lower()
    picked = []
    for skill in skills:
        if skill.problems or not skill.matches(surface=surface):
            continue
        named = (
            skill.name in lowered
            or any(h.split(".")[0] in lowered for h in skill.applies_to.get("hosts", []))
            or any(a.lower() in lowered for a in skill.applies_to.get("apps", []))
        )
        if (
            named
            or (host and skill.matches(host=host) and skill.applies_to.get("hosts"))
            or (app and skill.matches(app=app) and skill.applies_to.get("apps"))
        ):
            picked.append(skill)
    return picked[:limit]
