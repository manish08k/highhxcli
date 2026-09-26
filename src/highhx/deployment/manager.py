"""Deployment orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.config.schema import DeployTargetConfig, HighhXConfig
from highhx.core.engine import Engine
from highhx.core.errors import HighhXError, NotFoundError, ValidationError
from highhx.core.result import CheckResult, CheckStatus
from highhx.deployment.health import HealthResult, run_health_check
from highhx.deployment.rollback import rollback_pair
from highhx.deployment.state import DeploymentRecord, DeploymentStore, DeployStatus
from highhx.deployment.strategy import DeployContext, DeploymentStrategy, strategy_for
from highhx.deployment.target import describe, is_safe_value
from highhx.execution.command import CommandSpec
from highhx.git.repository import GitRepository
from highhx.policy.engine import PolicyEngine
from highhx.utils.validation import did_you_mean


@dataclass
class DeployOutcome:
    record: DeploymentRecord | None
    preflight: list[CheckResult] = field(default_factory=list)
    health: HealthResult | None = None
    rolled_back: bool = False
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.dry_run or (self.record is not None and self.record.status == DeployStatus.SUCCEEDED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "deployment": self.record.to_dict() if self.record else None,
            "preflight": [c.to_dict() for c in self.preflight],
            "health": self.health.to_dict() if self.health else None,
            "auto_rolled_back": self.rolled_back,
        }


class DeploymentManager:
    def __init__(
        self,
        engine: Engine,
        root: Path,
        config: HighhXConfig,
        store: DeploymentStore,
        repo: GitRepository | None = None,
        policy: PolicyEngine | None = None,
        version: str | None = None,
    ) -> None:
        self.engine = engine
        self.root = root
        self.config = config
        self.store = store
        self.repo = repo
        self.policy = policy or PolicyEngine()
        self.project_version = version

    # --------------------------------------------------------------- targets
    def targets(self) -> list[DeployTargetConfig]:
        return list(self.config.deploy_targets.values())

    def target(self, name: str | None) -> DeployTargetConfig:
        targets = self.config.deploy_targets
        if not targets:
            raise NotFoundError(
                "No deployment targets configured.",
                hint="Add deploy.targets to .highhx/config.yaml (see docs/configuration.md).",
            )
        chosen = name or self.config.deploy_default or (next(iter(targets)) if len(targets) == 1 else None)
        if chosen is None:
            raise ValidationError("Several targets are configured; name one.", hint=f"Targets: {', '.join(targets)}")
        if chosen not in targets:
            raise NotFoundError(
                f"Unknown deployment target '{chosen}'{did_you_mean(chosen, list(targets))}.",
                hint=f"Targets: {', '.join(targets)}",
            )
        return targets[chosen]

    def describe_targets(self) -> list[dict[str, Any]]:
        rows = []
        for target in self.targets():
            info = describe(target)
            latest = self.store.latest(target.name)
            info["last_deployment"] = latest.to_dict() if latest else None
            info["default"] = target.name == self.config.deploy_default
            rows.append(info)
        return rows

    # ------------------------------------------------------------- preflight
    def _git_facts(self) -> tuple[str | None, str | None, bool | None]:
        if self.repo is None or not self.repo.is_repo():
            return None, None, None
        return self.repo.current_branch(), self.repo.head(short=True), self.repo.status().clean

    def preflight(self, target: DeployTargetConfig, strategy: DeploymentStrategy | None = None) -> list[CheckResult]:
        strategy = strategy or strategy_for(self.engine, self.root, target)
        checks: list[CheckResult] = []
        problems = strategy.preflight()
        checks.append(
            CheckResult(
                "tools",
                CheckStatus.FAIL if problems else CheckStatus.OK,
                "; ".join(problems) or f"{target.type} strategy ready",
                category="deploy",
            )
        )
        branch, _sha, clean = self._git_facts()
        if target.require_branch:
            ok = branch == target.require_branch
            checks.append(
                CheckResult(
                    "branch",
                    CheckStatus.OK if ok else CheckStatus.FAIL,
                    f"on '{branch}', target requires '{target.require_branch}'",
                    category="deploy",
                )
            )
        if target.require_clean or self.policy.requires_clean_tree("deploy"):
            if clean is None:
                checks.append(
                    CheckResult(
                        "working tree", CheckStatus.WARN, "not a git repository; cannot verify", category="deploy"
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        "working tree",
                        CheckStatus.OK if clean else CheckStatus.FAIL,
                        "clean" if clean else "uncommitted changes",
                        hint="Commit or stash your changes.",
                        category="deploy",
                    )
                )
        if target.production and not target.health_check:
            checks.append(
                CheckResult(
                    "health check", CheckStatus.WARN, "production target without a health check", category="deploy"
                )
            )
        for command in target.preflight:
            if self.engine.dry_run:
                checks.append(CheckResult(f"preflight: {command}", CheckStatus.SKIP, "dry run", category="deploy"))
                continue
            result = self.engine.run(
                CommandSpec(command, cwd=self.root, env=target.env, name="preflight"),
                action=f"Preflight: {command}",
                source="preflight",
            )
            checks.append(
                CheckResult(
                    f"preflight: {command}",
                    CheckStatus.OK if result.ok else CheckStatus.FAIL,
                    f"exit code {result.exit_code}",
                    category="deploy",
                )
            )
            if not result.ok:
                break
        return checks

    # ---------------------------------------------------------------- deploy
    def deploy(
        self, name: str | None = None, *, version: str | None = None, skip_preflight: bool = False
    ) -> DeployOutcome:
        target = self.target(name)
        strategy = strategy_for(self.engine, self.root, target)
        checks = [] if skip_preflight else self.preflight(target, strategy)
        failed = [c for c in checks if c.status == CheckStatus.FAIL]
        if failed:
            raise ValidationError(
                f"Preflight checks failed for '{target.name}'",
                details=[f"{c.name}: {c.message}" for c in failed],
                hint="Fix the problems above, or run `highhx doctor`.",
            )
        branch, sha, _clean = self._git_facts()
        version = version or self.project_version or sha
        if version is not None and not is_safe_value(version):
            raise ValidationError(
                f"Invalid version label {version!r}.", hint="Use letters, digits and . _ + / : @ - only."
            )
        self.engine.approve(
            f"Deploy {version or 'current build'} to '{target.name}'" + (" (PRODUCTION)" if target.production else ""),
            strategy.risk,
            details=[f"type: {target.type}", f"branch: {branch or '-'}", f"commit: {sha or '-'}"],
            confirm_word=target.name if target.production else "yes",
            target=target.name,
            production=target.production,
            policy_action=f"deploy:{target.name}",
        )
        if self.engine.dry_run:
            return DeployOutcome(None, checks, dry_run=True)

        with self.engine.operation("deploy", target.name, metadata={"version": version, "git_sha": sha}) as op:
            record = self.store.create(
                target.name, target.type, version=version, git_sha=sha, execution_id=op.execution_id
            )
            previous = next((r for r in self.store.successful(target.name) if r.id != record.id), None)
            ctx = DeployContext(
                record.id,
                version,
                sha,
                previous.version if previous else None,
                previous.git_sha if previous else None,
                previous.details if previous else None,
            )
            try:
                details = strategy.deploy(ctx)
            except (HighhXError, KeyboardInterrupt) as exc:
                status = DeployStatus.CANCELLED if isinstance(exc, KeyboardInterrupt) else DeployStatus.FAILED
                self.store.finish(record.id, status, {"error": getattr(exc, "message", str(exc))})
                raise
            health = run_health_check(self.engine, target.health_check)
            details["health"] = health.to_dict()
            if not health.ok:
                self.store.finish(record.id, DeployStatus.FAILED, details)
                op.fail(f"health check failed: {health.message}")
                outcome = DeployOutcome(self.store.get(record.id), checks, health)
                if target.auto_rollback and previous is not None:
                    self._rollback(target, strategy, record, previous)
                    outcome.rolled_back = True
                    self.store.mark(record.id, DeployStatus.ROLLED_BACK)
                    outcome.record = self.store.get(record.id)
                return outcome
            self.store.finish(record.id, DeployStatus.SUCCEEDED, details)
            return DeployOutcome(self.store.get(record.id), checks, health)

    # -------------------------------------------------------------- rollback
    def _rollback(
        self,
        target: DeployTargetConfig,
        strategy: DeploymentStrategy,
        current: DeploymentRecord,
        previous: DeploymentRecord,
    ) -> DeploymentRecord:
        record = self.store.create(
            target.name,
            target.type,
            version=previous.version,
            git_sha=previous.git_sha,
            execution_id=None,
            rollback_of=current.id,
        )
        ctx = DeployContext(
            record.id, previous.version, previous.git_sha, previous.version, previous.git_sha, previous.details
        )
        try:
            details = strategy.rollback(ctx)
        except (HighhXError, KeyboardInterrupt) as exc:
            self.store.finish(record.id, DeployStatus.FAILED, {"error": getattr(exc, "message", str(exc))})
            raise
        health = run_health_check(self.engine, target.health_check)
        details["health"] = health.to_dict()
        details.update({k: v for k, v in previous.details.items() if k in ("image", "revision")})
        self.store.finish(record.id, DeployStatus.SUCCEEDED if health.ok else DeployStatus.FAILED, details)
        if health.ok:
            self.store.mark(current.id, DeployStatus.ROLLED_BACK)
        return self.store.get(record.id)

    def rollback(self, name: str | None = None, *, to: str | None = None) -> DeployOutcome:
        target = self.target(name)
        strategy = strategy_for(self.engine, self.root, target)
        current, previous = rollback_pair(self.store, target.name, to)
        self.engine.approve(
            f"Roll back '{target.name}' from {current.version or current.git_sha} to {previous.version or previous.git_sha}",
            strategy.risk,
            details=[f"current deployment: {current.id}", f"restore deployment: {previous.id}"],
            confirm_word=target.name if target.production else "yes",
            target=target.name,
            production=target.production,
            policy_action=f"rollback:{target.name}",
        )
        if self.engine.dry_run:
            return DeployOutcome(None, dry_run=True)
        with self.engine.operation("rollback", target.name) as op:
            record = self._rollback(target, strategy, current, previous)
            if record.status != DeployStatus.SUCCEEDED:
                op.fail("rollback health check failed")
            health = HealthResult(**record.details["health"]) if "health" in record.details else None
            return DeployOutcome(record, health=health)

    # ---------------------------------------------------------------- status
    def status(self, name: str | None = None, *, live: bool = True) -> list[dict[str, Any]]:
        names = [self.target(name).name] if name else [t.name for t in self.targets()]
        rows = []
        for target_name in names:
            target = self.config.deploy_targets[target_name]
            latest = self.store.latest(target_name)
            row: dict[str, Any] = {
                "target": target_name,
                "type": target.type,
                "production": target.production,
                "latest": latest.to_dict() if latest else None,
            }
            if live:
                try:
                    row["live"] = strategy_for(self.engine, self.root, target).status()
                except HighhXError as exc:
                    row["live"] = {"error": exc.message}
            rows.append(row)
        return rows

    def history(self, name: str | None = None, limit: int = 20) -> list[DeploymentRecord]:
        return self.store.history(self.target(name).name if name else None, limit=limit)

    def logs(self, name: str | None = None, *, follow: bool = False, tail: int = 200) -> bool:
        target = self.target(name)
        result = strategy_for(self.engine, self.root, target).logs(follow=follow, tail=tail)
        return result is not None
