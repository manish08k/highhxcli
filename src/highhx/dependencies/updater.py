"""Dependency updates (always previewed before running)."""

from __future__ import annotations

from pathlib import Path

from highhx.approvals.risk import RiskLevel
from highhx.core.engine import Engine
from highhx.core.errors import UsageError
from highhx.core.result import CommandResult
from highhx.dependencies.detector import ManagerAdapter
from highhx.execution.command import CommandSpec


def update_command(adapter: ManagerAdapter, packages: list[str]) -> list[str]:
    if packages:
        if adapter.update_packages is None:
            raise UsageError(f"{adapter.name} does not support updating individual packages through HighhX.")
        if adapter.name == "cargo":
            return ["cargo", "update", *[arg for pkg in packages for arg in ("-p", pkg)]]
        return [*adapter.update_packages, *packages]
    if adapter.update is None:
        raise UsageError(
            f"{adapter.name} has no safe 'update everything' command.",
            hint=adapter.notes or "Name the packages to update: highhx deps update <package>…",
        )
    return list(adapter.update)


def update(
    engine: Engine, root: Path, adapter: ManagerAdapter, packages: list[str], *, details: list[str]
) -> CommandResult:
    argv = update_command(adapter, packages)
    risk = RiskLevel.NORMAL if packages else RiskLevel.DANGEROUS
    what = ", ".join(packages) if packages else "all dependencies"
    lock = f" and rewrite {adapter.lockfile}" if adapter.lockfile else ""
    engine.approve(
        f"Update {what} with {adapter.name}{lock}",
        risk,
        details=details,
        policy_action="deps:update",
    )
    return engine.run(
        CommandSpec(argv, cwd=root, name=f"update:{adapter.name}"), approved=True, policy_action="deps:update"
    )
