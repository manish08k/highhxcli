"""Docker CLI wrapper."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.core.engine import Engine
from highhx.core.errors import IntegrationError, ToolNotFoundError
from highhx.core.result import CommandResult
from highhx.detection.container import find_compose_files
from highhx.execution.command import CommandSpec
from highhx.utils.processes import which


@dataclass
class ComposeService:
    name: str
    state: str
    status: str = ""
    ports: str = ""
    container: str = ""
    health: str = ""

    @property
    def running(self) -> bool:
        return self.state.lower() == "running"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__ | {"running": self.running}


def parse_compose_ps(text: str) -> list[ComposeService]:
    """Parse ``docker compose ps --format json`` (a JSON array or JSON lines)."""
    text = text.strip()
    if not text:
        return []
    items: list[dict[str, Any]] = []
    if text.startswith("["):
        items = json.loads(text)
    else:
        for line in text.splitlines():
            line = line.strip()
            if line:
                items.append(json.loads(line))
    services: list[ComposeService] = []
    for item in items:
        publishers = item.get("Publishers") or []
        ports = ", ".join(
            f"{p.get('URL') or '0.0.0.0'}:{p.get('PublishedPort')}->{p.get('TargetPort')}/{p.get('Protocol', 'tcp')}"
            for p in publishers
            if p.get("PublishedPort")
        ) or str(item.get("Ports") or "")
        services.append(
            ComposeService(
                name=str(item.get("Service") or item.get("Name") or "?"),
                state=str(item.get("State") or ""),
                status=str(item.get("Status") or ""),
                ports=ports,
                container=str(item.get("Name") or ""),
                health=str(item.get("Health") or ""),
            )
        )
    return services


class DockerClient:
    """Runs docker / docker compose through the engine."""

    def __init__(self, engine: Engine, root: Path, compose_file: str | None = None) -> None:
        self.engine = engine
        self.root = root
        self.compose_file = compose_file
        self._compose_cmd: list[str] | None = None

    # ---------------------------------------------------------- discovery
    @staticmethod
    def installed() -> bool:
        return which("docker") is not None

    def daemon_running(self) -> bool:
        if not self.installed():
            return False
        result = self.engine.capture(CommandSpec(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15))
        return result.ok and bool(result.stdout.strip())

    def require_daemon(self) -> None:
        if not self.installed():
            raise ToolNotFoundError(
                "docker", purpose="manage containers", hint="Install Docker Desktop or Docker Engine."
            )
        if not self.daemon_running():
            raise IntegrationError(
                "Docker is not running.",
                hint="Start Docker and retry, or run `highhx doctor`.",
            )

    def compose_command(self) -> list[str]:
        if self._compose_cmd is not None:
            return self._compose_cmd
        if self.installed():
            probe = self.engine.capture(CommandSpec(["docker", "compose", "version"], timeout=15))
            if probe.ok:
                self._compose_cmd = ["docker", "compose"]
                return self._compose_cmd
        if which("docker-compose"):
            self._compose_cmd = ["docker-compose"]
            return self._compose_cmd
        raise ToolNotFoundError(
            "docker compose", purpose="manage Compose services", hint="Install the Docker Compose plugin."
        )

    def compose_files(self) -> list[Path]:
        if self.compose_file:
            path = self.root / self.compose_file
            return [path] if path.is_file() else []
        return find_compose_files(self.root)

    def has_compose(self) -> bool:
        return bool(self.compose_files())

    def _compose(self, *args: str) -> list[str]:
        files = self.compose_files()
        if not files:
            raise IntegrationError(
                "No Docker Compose file found.",
                hint="Add compose.yaml / docker-compose.yml, or set compose_file in the deploy target.",
            )
        cmd = list(self.compose_command())
        if self.compose_file:
            cmd += ["-f", str(files[0])]
        return [*cmd, *args]

    # ------------------------------------------------------------ actions
    def compose_up(
        self, services: list[str] | None = None, *, build: bool = False, detach: bool = True
    ) -> CommandResult:
        self.require_daemon()
        args = ["up"]
        if detach:
            args.append("-d")
        if build:
            args.append("--build")
        args += services or []
        return self.engine.run(
            CommandSpec(self._compose(*args), cwd=self.root, interactive=not detach, name="docker-compose-up"),
            action="Start Docker Compose services",
            risk=RiskLevel.NORMAL,
            check=True,
            policy_action="docker:up",
        )

    def compose_down(self, *, volumes: bool = False, remove_orphans: bool = False) -> CommandResult:
        self.require_daemon()
        args = ["down"]
        if volumes:
            args.append("--volumes")
        if remove_orphans:
            args.append("--remove-orphans")
        return self.engine.run(
            CommandSpec(self._compose(*args), cwd=self.root, name="docker-compose-down"),
            action="Stop Docker Compose services" + (" and DELETE their volumes" if volumes else ""),
            risk=RiskLevel.CRITICAL if volumes else RiskLevel.NORMAL,
            check=True,
            policy_action="docker:down",
        )

    def compose_logs(
        self, services: list[str] | None = None, *, follow: bool = False, tail: int | None = 200
    ) -> CommandResult:
        self.require_daemon()
        args = ["logs", "--no-color"]
        if follow:
            args.append("--follow")
        if tail is not None:
            args += ["--tail", str(tail)]
        args += services or []
        return self.engine.run(
            CommandSpec(self._compose(*args), cwd=self.root, name="docker-compose-logs"),
            risk=RiskLevel.SAFE,
            record=False,
        )

    def compose_ps(self) -> list[ComposeService]:
        if not self.installed() or not self.has_compose():
            return []
        result = self.engine.capture(
            CommandSpec(self._compose("ps", "--all", "--format", "json"), cwd=self.root, timeout=30)
        )
        if not result.ok:
            return []
        try:
            return parse_compose_ps(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return []

    def build_image(self, tag: str, *, context: str = ".", dockerfile: str | None = None) -> CommandResult:
        self.require_daemon()
        args = ["docker", "build", "-t", tag]
        if dockerfile:
            args += ["-f", dockerfile]
        args.append(context)
        return self.engine.run(
            CommandSpec(args, cwd=self.root, name="docker-build"),
            risk=RiskLevel.NORMAL,
            check=True,
            policy_action="docker:build",
        )

    def image_exists(self, tag: str) -> bool:
        result = self.engine.capture(CommandSpec(["docker", "image", "inspect", tag], timeout=15))
        return result.ok

    def tag_image(self, source: str, target: str) -> CommandResult:
        return self.engine.run(
            CommandSpec(["docker", "tag", source, target], name="docker-tag"), risk=RiskLevel.NORMAL, check=True
        )
