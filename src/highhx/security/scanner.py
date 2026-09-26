"""Runs all local security checks and builds a :class:`SecurityReport`."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from highhx.config.schema import HighhXConfig
from highhx.policy.engine import PolicyEngine
from highhx.security import permissions, policies
from highhx.security.filesystem import scan_candidates
from highhx.security.report import Finding, SecurityReport
from highhx.security.secrets_scan import scan_file

ALL_CHECKS = ("secrets", "permissions", "config", "policy", "workflows", "dependencies")


@dataclass
class ScanContext:
    root: Path
    config: HighhXConfig
    policy: PolicyEngine
    tracked_files: Callable[[], list[str]] | None = None
    is_ignored: Callable[[str], bool] | None = None
    workflow_loader: object | None = None
    dependency_manager: object | None = None
    env_values: Callable[[], dict[str, dict[str, str]]] | None = None


class SecurityScanner:
    def __init__(self, ctx: ScanContext) -> None:
        self.ctx = ctx

    def run(self, checks: Iterable[str] = ALL_CHECKS, *, include_untracked: bool | None = None) -> SecurityReport:
        wanted = [c for c in ALL_CHECKS if c in set(checks)]
        report = SecurityReport()
        config = self.ctx.config
        untracked = config.security.scan_untracked if include_untracked is None else include_untracked
        files: list[Path] = []
        if {"secrets", "permissions"} & set(wanted):
            if self.ctx.tracked_files is None and not untracked:
                report.notes.append("Not a git repository: scanning all files instead of committed files.")
            files = scan_candidates(
                self.ctx.root,
                tracked=self.ctx.tracked_files,
                include_untracked=untracked or self.ctx.tracked_files is None,
                ignore=config.security.ignore,
                max_size=config.security.max_file_size,
            )
            report.files_scanned = len(files)
        for check in wanted:
            report.checks_run.append(check)
            if check == "secrets":
                for path in files:
                    for finding in scan_file(self.ctx.root, path):
                        report.add(finding)
            elif check == "permissions":
                for finding in permissions.check_permissions(self.ctx.root, files):
                    report.add(finding)
            elif check == "config":
                for finding in policies.config_findings(config):
                    report.add(finding)
                for finding in policies.gitignore_findings(self.ctx.root, self.ctx.is_ignored):
                    report.add(finding)
                if self.ctx.env_values is not None:
                    for finding in policies.env_profile_findings(self.ctx.env_values()):
                        report.add(finding)
            elif check == "policy":
                if self.ctx.tracked_files is not None:
                    for finding in policies.committed_file_findings(self.ctx.tracked_files(), self.ctx.policy):
                        report.add(finding)
                else:
                    report.notes.append("policy: committed-file checks need a git repository")
            elif check == "workflows":
                if self.ctx.workflow_loader is not None:
                    for finding in policies.workflow_findings(self.ctx.workflow_loader):  # type: ignore[arg-type]
                        report.add(finding)
            elif check == "dependencies":
                if self.ctx.dependency_manager is None:
                    report.notes.append("dependencies: no supported package manager detected")
                    continue
                from highhx.core.errors import HighhXError
                from highhx.security.dependencies import dependency_findings

                try:
                    found, notes = dependency_findings(self.ctx.dependency_manager)  # type: ignore[arg-type]
                except HighhXError as exc:
                    report.notes.append(f"dependencies: {exc.message}")
                    continue
                for finding in found:
                    report.add(finding)
                report.notes.extend(notes)
        allow = set(config.security.allowlist)
        if allow:
            kept: list[Finding] = [f for f in report.findings if f.fingerprint not in allow]
            report.suppressed = len(report.findings) - len(kept)
            report.findings = kept
        return report
