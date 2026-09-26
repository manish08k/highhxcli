"""Terraform CLI wrapper. ``apply`` always requires HighhX approval first."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.core.engine import Engine
from highhx.core.errors import ToolNotFoundError
from highhx.core.result import CommandResult
from highhx.execution.command import CommandSpec
from highhx.utils.processes import which


class TerraformClient:
    def __init__(self, engine: Engine, directory: Path, *, variables: dict[str, str] | None = None) -> None:
        self.engine = engine
        self.directory = directory
        self.variables = variables or {}

    def require(self) -> None:
        if which("terraform") is None:
            raise ToolNotFoundError("terraform", purpose="manage infrastructure")

    def _vars(self) -> list[str]:
        return [arg for key, value in sorted(self.variables.items()) for arg in ("-var", f"{key}={value}")]

    def init(self) -> CommandResult:
        self.require()
        return self.engine.run(
            CommandSpec(["terraform", "init", "-input=false"], cwd=self.directory, name="terraform-init"),
            risk=RiskLevel.NORMAL,
            check=True,
        )

    def plan(self, out: str = "highhx.tfplan") -> CommandResult:
        self.require()
        return self.engine.run(
            CommandSpec(
                ["terraform", "plan", "-input=false", f"-out={out}", *self._vars()],
                cwd=self.directory,
                name="terraform-plan",
            ),
            risk=RiskLevel.SAFE,
            check=True,
        )

    def apply(
        self, plan_file: str = "highhx.tfplan", *, approved: bool = False, production: bool = False
    ) -> CommandResult:
        self.require()
        return self.engine.run(
            CommandSpec(["terraform", "apply", "-input=false", plan_file], cwd=self.directory, name="terraform-apply"),
            risk=RiskLevel.CRITICAL,
            approved=approved,
            production=production,
        )

    def outputs(self) -> dict[str, Any]:
        result = self.engine.capture(CommandSpec(["terraform", "output", "-json"], cwd=self.directory, timeout=60))
        if not result.ok:
            return {}
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return {}
        return {k: v.get("value") for k, v in data.items() if isinstance(v, dict)}
