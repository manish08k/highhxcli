"""``highhx init``: create the ``.highhx/`` directory from templates."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.config.defaults import GITIGNORE_ENTRIES
from highhx.core.engine import Engine
from highhx.project.context import ProjectPaths
from highhx.project.detector import ProjectProfile
from highhx.utils.filesystem import atomic_write_text, ensure_dir, ensure_gitignore_entries
from highhx.workflows.templates import TemplateLibrary

HOOKS_README = """# HighhX hooks

Scripts here run when the matching git hook fires, after `highhx hook install <hook>`.
Name a script after the hook, e.g. `pre-commit.sh` or `pre-push.py`.
"""


@dataclass
class InitPlan:
    root: Path
    stack: str
    files: dict[str, str]
    create: list[str] = field(default_factory=list)
    overwrite: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    gitignore: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "stack": self.stack,
            "create": self.create,
            "overwrite": self.overwrite,
            "skip": self.skip,
            "gitignore": self.gitignore,
        }


def template_context(profile: ProjectProfile, commands: dict[str, str] | None = None) -> dict[str, Any]:
    """Values for template placeholders; ``commands`` (effective config) overrides detection."""
    members = (profile.monorepo.patterns or profile.monorepo.members) if profile.monorepo else []
    return {
        "project": {"name": profile.name, "type": profile.primary or "generic"},
        "commands": {**profile.commands, **(commands or {})},
        "workspace": {"members": members},
    }


def plan_init(
    profile: ProjectProfile,
    *,
    force: bool = False,
    stack: str | None = None,
    library: TemplateLibrary | None = None,
    commands: dict[str, str] | None = None,
) -> InitPlan:
    library = library or TemplateLibrary()
    chosen = stack or profile.template
    rendered = library.render_project(chosen, template_context(profile, commands))
    paths = ProjectPaths(profile.root)
    plan = InitPlan(profile.root, chosen, {})
    for rel, content in rendered.files.items():
        target = paths.config_dir / rel
        key = f".highhx/{rel}"
        plan.files[key] = content
        if target.exists():
            (plan.overwrite if force else plan.skip).append(key)
        else:
            plan.create.append(key)
    readme = paths.hooks_dir / "README.md"
    if not readme.exists():
        plan.files[".highhx/hooks/README.md"] = HOOKS_README
        plan.create.append(".highhx/hooks/README.md")
    if profile.git:
        gitignore = profile.root / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8").split() if gitignore.exists() else []
        plan.gitignore = [e for e in GITIGNORE_ENTRIES if e not in existing]
    return plan


def apply_init(engine: Engine, plan: InitPlan) -> InitPlan:
    if plan.overwrite:
        engine.approve(
            "Overwrite existing HighhX configuration",
            RiskLevel.DANGEROUS,
            details=plan.overwrite,
            policy_action="init:overwrite",
        )
    if engine.dry_run:
        return plan
    paths = ProjectPaths(plan.root)
    for directory in (paths.config_dir, paths.workflows_dir, paths.hooks_dir, paths.state_dir, paths.logs_dir):
        ensure_dir(directory)
    for rel in [*plan.create, *plan.overwrite]:
        atomic_write_text(plan.root / rel, plan.files[rel])
    if plan.gitignore:
        ensure_gitignore_entries(plan.root / ".gitignore", plan.gitignore)
    return plan
