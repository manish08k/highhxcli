"""Deployment strategies (one per target type). Plugins can register more."""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.config.schema import DeployTargetConfig
from highhx.core.engine import Engine
from highhx.core.errors import IntegrationError, ValidationError
from highhx.core.result import CommandResult
from highhx.deployment.target import render_command
from highhx.execution.command import CommandSpec
from highhx.utils.processes import which


@dataclass
class DeployContext:
    deployment_id: str
    version: str | None
    git_sha: str | None
    previous_version: str | None = None
    previous_git_sha: str | None = None
    previous_details: dict[str, Any] | None = None

    def values(self, target: DeployTargetConfig) -> dict[str, str | None]:
        return {
            "version": self.version,
            "git_sha": self.git_sha,
            "previous_version": self.previous_version,
            "previous_git_sha": self.previous_git_sha,
            "target": target.name,
            "deployment_id": self.deployment_id,
        }


class DeploymentStrategy(abc.ABC):
    type: str = "abstract"
    tools: tuple[str, ...] = ()

    def __init__(self, engine: Engine, root: Path, target: DeployTargetConfig) -> None:
        self.engine = engine
        self.root = root
        self.target = target

    @property
    def risk(self) -> RiskLevel:
        return RiskLevel.CRITICAL if self.target.production else RiskLevel.DANGEROUS

    def preflight(self) -> list[str]:
        return [f"`{tool}` is not installed" for tool in self.tools if which(tool) is None]

    def _render(self, command: str, ctx: DeployContext) -> str:
        try:
            return render_command(command, ctx.values(self.target))
        except ValueError as exc:
            raise ValidationError(f"Target '{self.target.name}': {exc}") from exc

    def _run_local(self, command: str, name: str) -> CommandResult:
        result = self.engine.run(
            CommandSpec(command, cwd=self.root, env=self.target.env, timeout=self.target.timeout, name=name),
            approved=True,
            production=self.target.production,
            target=self.target.name,
            policy_action=f"deploy:{self.target.name}",
        )
        self.engine.raise_for(result)
        return result

    @abc.abstractmethod
    def deploy(self, ctx: DeployContext) -> dict[str, Any]:
        """Perform the deployment; raise on failure; return details to store."""

    def rollback(self, ctx: DeployContext) -> dict[str, Any]:
        if not self.target.rollback_command:
            raise ValidationError(
                f"Target '{self.target.name}' has no rollback_command.",
                hint="Add rollback_command (it can use {{ previous_version }} and {{ previous_git_sha }}).",
            )
        self._run_local(self._render(self.target.rollback_command, ctx), "rollback")
        return {"rollback_command": True}

    def status(self) -> dict[str, Any]:
        if not self.target.status_command:
            return {"live": None, "note": "no status_command configured"}
        result = self.engine.capture(
            CommandSpec(self.target.status_command, cwd=self.root, env=self.target.env, timeout=60)
        )
        return {"live": result.ok, "output": result.stdout.strip()[-2000:], "exit_code": result.exit_code}

    def logs(self, *, follow: bool = False, tail: int = 200) -> CommandResult | None:
        if not self.target.logs_command:
            return None
        return self.engine.run(
            CommandSpec(self.target.logs_command, cwd=self.root, env=self.target.env, name="deploy-logs"),
            risk=RiskLevel.SAFE,
            record=False,
        )


class LocalStrategy(DeploymentStrategy):
    """Runs ``command`` on this machine."""

    type = "local"

    def deploy(self, ctx: DeployContext) -> dict[str, Any]:
        assert self.target.command
        command = self._render(self.target.command, ctx)
        result = self._run_local(command, "deploy")
        return {"command": command, "duration": round(result.duration, 3)}


class SSHStrategy(DeploymentStrategy):
    """Runs ``command`` on a remote host through the system OpenSSH client."""

    type = "ssh"
    tools = ("ssh",)

    def _client(self):  # type: ignore[no-untyped-def]
        from highhx.integrations.ssh import SSHClient

        assert self.target.host
        return SSHClient(
            self.engine,
            self.target.host,
            port=self.target.port,
            user=self.target.user,
            identity_file=self.target.identity_file,
        )

    def _remote(self, command: str) -> str:
        if not self.target.remote_dir:
            return command
        import shlex

        # The remote side is a POSIX shell; quote the directory.
        return f"cd {shlex.quote(self.target.remote_dir)} && {command}"

    def _exec(self, command: str, name: str) -> CommandResult:
        result = self._client().run(
            self._remote(command),
            risk=self.risk,
            timeout=self.target.timeout,
            approved=True,
            production=self.target.production,
            name=name,
        )
        self.engine.raise_for(result, hint="Check SSH connectivity with `ssh <host> true`.")
        return result

    def deploy(self, ctx: DeployContext) -> dict[str, Any]:
        assert self.target.command
        command = self._render(self.target.command, ctx)
        self._exec(command, "deploy")
        return {"host": self.target.host, "command": command}

    def rollback(self, ctx: DeployContext) -> dict[str, Any]:
        if not self.target.rollback_command:
            return super().rollback(ctx)
        self._exec(self._render(self.target.rollback_command, ctx), "rollback")
        return {"host": self.target.host}

    def status(self) -> dict[str, Any]:
        if not self.target.status_command:
            probe = self._client().check_connection()
            return {"live": None, "reachable": probe.ok}
        result = self.engine.capture(
            CommandSpec(self._client().argv(self._remote(self.target.status_command)), timeout=60)
        )
        return {"live": result.ok, "output": result.stdout.strip()[-2000:]}

    def logs(self, *, follow: bool = False, tail: int = 200) -> CommandResult | None:
        if not self.target.logs_command:
            return None
        return self.engine.run(
            CommandSpec(self._client().argv(self._remote(self.target.logs_command)), name="deploy-logs"),
            risk=RiskLevel.SAFE,
            record=False,
        )


class DockerStrategy(DeploymentStrategy):
    """Docker Compose deployment (optionally tagging an image per version)."""

    type = "docker"
    tools = ("docker",)

    def _client(self):  # type: ignore[no-untyped-def]
        from highhx.integrations.docker import DockerClient

        return DockerClient(self.engine, self.root, self.target.compose_file)

    def preflight(self) -> list[str]:
        problems = super().preflight()
        client = self._client()
        if not problems and not client.daemon_running():
            problems.append("Docker daemon is not running")
        if not self.target.command and not client.has_compose() and not self.target.image:
            problems.append("no compose file, image or command configured")
        return problems

    def deploy(self, ctx: DeployContext) -> dict[str, Any]:
        if self.target.command:
            command = self._render(self.target.command, ctx)
            self._run_local(command, "deploy")
            return {"command": command}
        client = self._client()
        details: dict[str, Any] = {}
        if self.target.image:
            tag = f"{self.target.image}:{ctx.version or ctx.git_sha or 'latest'}"
            client.build_image(tag)
            client.tag_image(tag, f"{self.target.image}:latest")
            details["image"] = tag
        if client.has_compose():
            services = [self.target.service] if self.target.service else None
            client.compose_up(services, build=self.target.build and not self.target.image)
            details["compose"] = True
        return details

    def rollback(self, ctx: DeployContext) -> dict[str, Any]:
        if self.target.rollback_command:
            return super().rollback(ctx)
        previous_image = (ctx.previous_details or {}).get("image")
        if not self.target.image or not previous_image:
            raise ValidationError(
                f"Target '{self.target.name}' cannot roll back automatically.",
                hint="Set `image` (versioned images are kept) or a rollback_command.",
            )
        client = self._client()
        if not client.image_exists(previous_image):
            raise IntegrationError(f"Previous image {previous_image} no longer exists locally.")
        client.tag_image(previous_image, f"{self.target.image}:latest")
        if client.has_compose():
            client.compose_up([self.target.service] if self.target.service else None)
        return {"image": previous_image}

    def status(self) -> dict[str, Any]:
        services = self._client().compose_ps()
        return {
            "live": any(s.running for s in services) if services else None,
            "services": [s.to_dict() for s in services],
        }

    def logs(self, *, follow: bool = False, tail: int = 200) -> CommandResult | None:
        return self._client().compose_logs(
            [self.target.service] if self.target.service else None, follow=follow, tail=tail
        )


class KubernetesStrategy(DeploymentStrategy):
    type = "kubernetes"
    tools = ("kubectl",)

    def _client(self):  # type: ignore[no-untyped-def]
        from highhx.integrations.kubernetes import KubectlClient

        return KubectlClient(self.engine, self.root, context=self.target.context, namespace=self.target.namespace)

    def deploy(self, ctx: DeployContext) -> dict[str, Any]:
        client = self._client()
        details: dict[str, Any] = {}
        if self.target.deployment:
            details["previous_revision"] = client.current_revision(self.target.deployment)
        if self.target.command:
            self._run_local(self._render(self.target.command, ctx), "deploy")
        else:
            assert self.target.manifests
            self.engine.raise_for(client.apply(self.target.manifests, production=self.target.production))
        if self.target.deployment:
            self.engine.raise_for(client.rollout_status(self.target.deployment, timeout=self.target.timeout or 300))
            details["revision"] = client.current_revision(self.target.deployment)
        return details

    def rollback(self, ctx: DeployContext) -> dict[str, Any]:
        if self.target.rollback_command:
            return super().rollback(ctx)
        if not self.target.deployment:
            raise ValidationError(f"Target '{self.target.name}' needs `deployment` (or rollback_command) to roll back.")
        client = self._client()
        revision = (ctx.previous_details or {}).get("revision")
        self.engine.raise_for(client.rollout_undo(self.target.deployment, revision=revision))
        self.engine.raise_for(client.rollout_status(self.target.deployment, timeout=self.target.timeout or 300))
        return {"revision": client.current_revision(self.target.deployment)}

    def status(self) -> dict[str, Any]:
        result = self._client().status(self.target.deployment)
        return {"live": result.ok, "output": result.stdout.strip()[-2000:]}

    def logs(self, *, follow: bool = False, tail: int = 200) -> CommandResult | None:
        if not self.target.deployment:
            return super().logs(follow=follow, tail=tail)
        return self._client().logs(self.target.deployment, follow=follow, tail=tail)


class TerraformStrategy(DeploymentStrategy):
    type = "terraform"
    tools = ("terraform",)

    def deploy(self, ctx: DeployContext) -> dict[str, Any]:
        from highhx.integrations.terraform import TerraformClient

        client = TerraformClient(
            self.engine,
            self.root / (self.target.directory or "."),
            variables={
                **self.target.vars,
                **({"version": ctx.version} if ctx.version and "version" in self.target.vars else {}),
            },
        )
        client.init()
        client.plan()
        self.engine.raise_for(client.apply(approved=True, production=self.target.production))
        return {"outputs": client.outputs()}

    def rollback(self, ctx: DeployContext) -> dict[str, Any]:
        if not self.target.rollback_command:
            raise ValidationError(
                "Terraform changes cannot be rolled back automatically.",
                hint="Configure rollback_command (e.g. re-apply the previous git revision).",
            )
        return super().rollback(ctx)


class PluginStrategy(DeploymentStrategy):
    """Delegates to a cloud provider registered by a plugin (type ``plugin:<name>``)."""

    type = "plugin"

    def _provider(self):  # type: ignore[no-untyped-def]
        from highhx.integrations.cloud import get_provider, provider_names

        name = self.target.type.split(":", 1)[1]
        provider = get_provider(name)
        if provider is None:
            raise IntegrationError(
                f"No plugin provides deployment backend '{name}'.",
                hint=f"Registered providers: {', '.join(provider_names()) or 'none'}. Install/enable the plugin.",
            )
        return provider

    def preflight(self) -> list[str]:
        try:
            self._provider()
        except IntegrationError as exc:
            return [exc.message]
        return []

    def deploy(self, ctx: DeployContext) -> dict[str, Any]:
        return dict(
            self._provider().deploy(
                self.target, {"version": ctx.version, "git_sha": ctx.git_sha, "engine": self.engine}
            )
        )

    def rollback(self, ctx: DeployContext) -> dict[str, Any]:
        return dict(self._provider().rollback(self.target, ctx.previous_details or {}, {"engine": self.engine}))

    def status(self) -> dict[str, Any]:
        return dict(self._provider().status(self.target, {"engine": self.engine}))


StrategyFactory = Callable[[Engine, Path, DeployTargetConfig], DeploymentStrategy]

_STRATEGIES: dict[str, StrategyFactory] = {
    "local": LocalStrategy,
    "ssh": SSHStrategy,
    "docker": DockerStrategy,
    "kubernetes": KubernetesStrategy,
    "terraform": TerraformStrategy,
}


def register_strategy(type_name: str, factory: StrategyFactory) -> None:
    _STRATEGIES[type_name] = factory


def strategy_for(engine: Engine, root: Path, target: DeployTargetConfig) -> DeploymentStrategy:
    if target.type.startswith("plugin:"):
        return PluginStrategy(engine, root, target)
    factory = _STRATEGIES.get(target.type)
    if factory is None:
        raise ValidationError(f"Unknown deployment type '{target.type}'.")
    return factory(engine, root, target)
