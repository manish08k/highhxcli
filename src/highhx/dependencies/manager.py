"""Dependency manager service."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.config.schema import HighhXConfig
from highhx.core.engine import Engine
from highhx.core.errors import NotFoundError, UsageError
from highhx.core.result import CommandResult
from highhx.dependencies import installer, updater
from highhx.dependencies.audit import Vulnerability, parse_audit
from highhx.dependencies.detector import ManagerAdapter, detect_adapters
from highhx.dependencies.outdated import OutdatedPackage, parse_outdated
from highhx.execution.command import CommandSpec
from highhx.project.detector import ProjectProfile
from highhx.utils.filesystem import human_size, path_size, remove_path
from highhx.utils.paths import is_within
from highhx.utils.processes import which


@dataclass
class OutdatedReport:
    manager: str
    packages: list[OutdatedPackage] = field(default_factory=list)
    error: str | None = None
    supported: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "manager": self.manager,
            "supported": self.supported,
            "error": self.error,
            "packages": [p.to_dict() for p in self.packages],
        }


@dataclass
class AuditReport:
    manager: str
    tool: str | None
    vulnerabilities: list[Vulnerability] = field(default_factory=list)
    available: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "manager": self.manager,
            "tool": self.tool,
            "available": self.available,
            "error": self.error,
            "vulnerabilities": [v.to_dict() for v in self.vulnerabilities],
        }


class DependencyManager:
    def __init__(self, engine: Engine, root: Path, profile: ProjectProfile, config: HighhXConfig | None = None) -> None:
        self.engine = engine
        self.root = root
        self.profile = profile
        self.config = config or HighhXConfig()
        self.adapters = detect_adapters(root, profile)

    def select(self, name: str | None = None) -> list[ManagerAdapter]:
        if not self.adapters:
            raise NotFoundError(
                "No supported package manager detected in this project.",
                hint="HighhX looks for pyproject.toml, requirements.txt, package.json, pubspec.yaml, pom.xml, build.gradle, Cargo.toml or go.mod.",
            )
        if name is None:
            return self.adapters
        chosen = [a for a in self.adapters if name in (a.name, a.ecosystem)]
        if not chosen:
            raise NotFoundError(
                f"Package manager '{name}' is not used in this project.",
                hint=f"Detected: {', '.join(a.name for a in self.adapters)}",
            )
        return chosen

    def summary(self) -> list[dict[str, Any]]:
        rows = []
        for adapter in self.adapters:
            lock = self.root / adapter.lockfile if adapter.lockfile else None
            rows.append(
                {
                    "manager": adapter.name,
                    "ecosystem": adapter.ecosystem,
                    "available": adapter.available,
                    "lockfile": adapter.lockfile,
                    "lockfile_present": bool(lock and lock.exists()),
                    "install": " ".join(adapter.install),
                }
            )
        return rows

    def install(self, manager: str | None = None) -> list[CommandResult]:
        results = []
        configured = self.config.commands.get("install")
        if configured and manager is None:
            return [
                installer.install(
                    self.engine,
                    self.root,
                    self.adapters[0] if self.adapters else ManagerAdapter("custom", "custom", "", []),
                    command=configured,
                )
            ]
        for adapter in self.select(manager):
            result = installer.install(self.engine, self.root, adapter)
            results.append(result)
            if not result.ok and not result.dry_run:
                break
        return results

    def outdated(self, manager: str | None = None) -> list[OutdatedReport]:
        reports = []
        for adapter in self.select(manager):
            if adapter.outdated is None or adapter.outdated_format is None:
                reports.append(OutdatedReport(adapter.name, supported=False, error=adapter.notes or "not supported"))
                continue
            if not adapter.available:
                reports.append(OutdatedReport(adapter.name, error=f"{adapter.executable} is not installed"))
                continue
            result = self.engine.capture(CommandSpec(adapter.outdated, cwd=self.root, timeout=300))
            # npm/yarn exit 1 when packages are outdated; judge by parseable output instead.
            try:
                packages = parse_outdated(adapter.outdated_format, result.stdout)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                reports.append(
                    OutdatedReport(adapter.name, error=(result.stderr.strip().splitlines() or [str(exc)])[-1])
                )
                continue
            if not packages and result.exit_code not in (0, 1):
                reports.append(
                    OutdatedReport(
                        adapter.name,
                        error=(result.stderr.strip().splitlines() or [f"exit code {result.exit_code}"])[-1],
                    )
                )
                continue
            reports.append(OutdatedReport(adapter.name, packages))
        return reports

    def update(self, packages: list[str], manager: str | None = None) -> list[CommandResult]:
        adapters = self.select(manager)
        if packages and len(adapters) > 1:
            raise UsageError(
                "Several package managers are in use; choose one with --manager.",
                hint=", ".join(a.name for a in adapters),
            )
        results = []
        for adapter in adapters:
            preview: list[str] = []
            if not packages:
                for report in self.outdated(adapter.name):
                    preview += [
                        f"{p.name}: {p.current} → {p.wanted or p.latest} ({p.update_type})" for p in report.packages
                    ]
                if not preview:
                    preview = ["no outdated packages reported (lockfile may still be refreshed)"]
            results.append(updater.update(self.engine, self.root, adapter, packages, details=preview[:40]))
        return results

    def audit(self, manager: str | None = None) -> list[AuditReport]:
        reports = []
        for adapter in self.select(manager):
            if adapter.audit is None or adapter.audit_format is None:
                reports.append(
                    AuditReport(adapter.name, None, available=False, error="no local audit tool for this ecosystem")
                )
                continue
            tool = adapter.audit[0]
            if which(tool) is None:
                hint = (
                    "pip install pip-audit"
                    if tool == "pip-audit"
                    else "cargo install cargo-audit"
                    if adapter.name == "cargo"
                    else f"install {tool}"
                )
                reports.append(
                    AuditReport(adapter.name, tool, available=False, error=f"{tool} is not installed ({hint})")
                )
                continue
            result = self.engine.capture(CommandSpec(adapter.audit, cwd=self.root, timeout=600))
            try:
                vulns = parse_audit(adapter.audit_format, result.stdout)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                reports.append(
                    AuditReport(
                        adapter.name,
                        tool,
                        error=(result.stderr.strip().splitlines() or ["could not parse audit output"])[-1],
                    )
                )
                continue
            if not vulns and result.exit_code not in (0, 1):
                reports.append(
                    AuditReport(
                        adapter.name,
                        tool,
                        error=(result.stderr.strip().splitlines() or [f"exit code {result.exit_code}"])[-1],
                    )
                )
                continue
            reports.append(AuditReport(adapter.name, tool, vulns))
        return reports

    def clean_targets(self, manager: str | None = None) -> list[tuple[Path, int]]:
        targets = []
        for adapter in self.select(manager):
            for rel in adapter.clean_paths:
                path = self.root / rel
                if path.exists() and is_within(path, self.root) and not path.is_symlink():
                    targets.append((path, path_size(path)))
        return targets

    def clean(self, manager: str | None = None) -> list[Path]:
        targets = self.clean_targets(manager)
        if not targets:
            return []
        details = [f"{p.relative_to(self.root).as_posix()} ({human_size(size)})" for p, size in targets]
        self.engine.approve(
            "Delete installed dependency directories", RiskLevel.DANGEROUS, details=details, policy_action="deps:clean"
        )
        if self.engine.dry_run:
            return [p for p, _ in targets]
        removed = []
        with self.engine.operation("deps", "clean"):
            for path, _ in targets:
                remove_path(path)
                removed.append(path)
        return removed
