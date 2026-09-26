"""Project builds."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.building.artifacts import Artifact, find_artifacts, write_manifest
from highhx.config.schema import HighhXConfig
from highhx.core.engine import Engine
from highhx.core.errors import NotFoundError
from highhx.core.result import CommandResult
from highhx.execution.command import CommandSpec
from highhx.project.detector import ProjectProfile


@dataclass
class BuildOutcome:
    command: str
    result: CommandResult
    artifacts: list[Artifact] = field(default_factory=list)
    new_artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "status": str(self.result.status),
            "exit_code": self.result.exit_code,
            "duration": round(self.result.duration, 3),
            "artifacts": [a.to_dict() for a in self.artifacts],
            "new_artifacts": self.new_artifacts,
        }


class Builder:
    def __init__(
        self, engine: Engine, root: Path, profile: ProjectProfile, config: HighhXConfig, state_dir: Path | None
    ) -> None:
        self.engine = engine
        self.root = root
        self.profile = profile
        self.config = config
        self.state_dir = state_dir

    def command_for(self, kind: str = "build") -> str:
        command = self.config.commands.get(kind) or self.profile.commands.get(kind)
        if kind == "package" and not command:
            from highhx.building.packaging import default_package_command

            command = default_package_command(self.profile)
        if not command:
            raise NotFoundError(
                f"No {kind} command configured or detected.",
                hint=f"Set commands.{kind} in .highhx/config.yaml.",
            )
        return command

    def _run(self, kind: str) -> BuildOutcome:
        command = self.command_for(kind)
        before = {a.path: a.sha256 for a in find_artifacts(self.root)}
        result = self.engine.run(
            CommandSpec(command, cwd=self.root, name=kind), action=f"{kind.capitalize()}: {command}", policy_action=kind
        )
        artifacts = find_artifacts(self.root) if result.ok else []
        if result.ok and self.state_dir is not None and not result.dry_run:
            write_manifest(self.state_dir, artifacts)
        new = [a.path for a in artifacts if before.get(a.path) != a.sha256]
        return BuildOutcome(command, result, artifacts, new)

    def build(self) -> BuildOutcome:
        return self._run("build")

    def package(self) -> BuildOutcome:
        return self._run("package")
