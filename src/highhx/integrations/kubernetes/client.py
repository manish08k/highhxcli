"""kubectl wrapper."""

from __future__ import annotations

from pathlib import Path

from highhx.approvals.risk import RiskLevel
from highhx.core.engine import Engine
from highhx.core.errors import ToolNotFoundError
from highhx.core.result import CommandResult
from highhx.execution.command import CommandSpec
from highhx.utils.processes import which


class KubectlClient:
    """Thin wrapper; the kube context/namespace are always explicit when configured."""

    def __init__(self, engine: Engine, root: Path, *, context: str | None = None, namespace: str | None = None) -> None:
        self.engine = engine
        self.root = root
        self.context = context
        self.namespace = namespace

    def require(self) -> None:
        if which("kubectl") is None:
            raise ToolNotFoundError("kubectl", purpose="deploy to Kubernetes")

    def base(self) -> list[str]:
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        if self.namespace:
            cmd += ["--namespace", self.namespace]
        return cmd

    def apply(self, manifests: str, *, production: bool = False) -> CommandResult:
        self.require()
        path = self.root / manifests
        flag = ["-k", str(path)] if (path / "kustomization.yaml").is_file() else ["-f", str(path)]
        return self.engine.run(
            CommandSpec([*self.base(), "apply", *flag], cwd=self.root, name="kubectl-apply"),
            risk=RiskLevel.CRITICAL if production else RiskLevel.DANGEROUS,
            production=production,
            approved=True,
        )

    def rollout_status(self, deployment: str, *, timeout: float = 300) -> CommandResult:
        self.require()
        return self.engine.run(
            CommandSpec(
                [*self.base(), "rollout", "status", f"deployment/{deployment}", f"--timeout={int(timeout)}s"],
                name="kubectl-rollout-status",
            ),
            risk=RiskLevel.SAFE,
            record=False,
        )

    def rollout_undo(self, deployment: str, *, revision: str | None = None) -> CommandResult:
        self.require()
        args = [*self.base(), "rollout", "undo", f"deployment/{deployment}"]
        if revision:
            args.append(f"--to-revision={revision}")
        return self.engine.run(CommandSpec(args, name="kubectl-rollout-undo"), risk=RiskLevel.DANGEROUS, approved=True)

    def current_revision(self, deployment: str) -> str | None:
        result = self.engine.capture(
            CommandSpec(
                [
                    *self.base(),
                    "get",
                    f"deployment/{deployment}",
                    "-o",
                    "jsonpath={.metadata.annotations.deployment\\.kubernetes\\.io/revision}",
                ],
                timeout=30,
            )
        )
        value = result.stdout.strip()
        return value if result.ok and value else None

    def status(self, deployment: str | None) -> CommandResult:
        target = [f"deployment/{deployment}"] if deployment else ["deployments"]
        return self.engine.capture(CommandSpec([*self.base(), "get", *target, "-o", "wide"], timeout=30))

    def logs(self, deployment: str, *, follow: bool = False, tail: int = 200) -> CommandResult:
        args = [*self.base(), "logs", f"deployment/{deployment}", f"--tail={tail}"]
        if follow:
            args.append("--follow")
        return self.engine.run(CommandSpec(args, name="kubectl-logs"), risk=RiskLevel.SAFE, record=False)
