"""Dependency installation."""

from __future__ import annotations

from pathlib import Path

from highhx.approvals.risk import RiskLevel
from highhx.core.engine import Engine
from highhx.core.errors import ToolNotFoundError
from highhx.core.result import CommandResult
from highhx.dependencies.detector import ManagerAdapter
from highhx.execution.command import CommandSpec


def install(engine: Engine, root: Path, adapter: ManagerAdapter, *, command: str | None = None) -> CommandResult:
    """Install dependencies exactly as declared (lockfile-respecting where supported)."""
    if command is None and not adapter.available:
        raise ToolNotFoundError(adapter.executable, purpose=f"install {adapter.ecosystem} dependencies")
    spec = CommandSpec(command if command else adapter.install, cwd=root, name=f"install:{adapter.name}")
    return engine.run(
        spec,
        action=f"Install {adapter.ecosystem} dependencies with {adapter.name}",
        risk=RiskLevel.NORMAL,
        policy_action="deps:install",
    )
