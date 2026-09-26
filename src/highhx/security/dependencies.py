"""Dependency vulnerability findings from locally available audit tools."""

from __future__ import annotations

from highhx.dependencies.manager import DependencyManager
from highhx.security.report import Finding

SEVERITY = {
    "critical": "critical",
    "high": "high",
    "moderate": "medium",
    "medium": "medium",
    "low": "low",
    "info": "info",
    "unknown": "medium",
}


def dependency_findings(manager: DependencyManager) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    notes: list[str] = []
    for report in manager.audit():
        if not report.available or report.error:
            notes.append(f"{report.manager}: dependency audit skipped — {report.error}")
            continue
        for vuln in report.vulnerabilities:
            fix = f"upgrade to {', '.join(vuln.fix_versions)}" if vuln.fix_versions else "no fixed version reported"
            findings.append(
                Finding(
                    rule=f"vuln:{vuln.id}",
                    severity=SEVERITY.get(vuln.severity, "medium"),
                    title=f"{vuln.package} {vuln.version or ''} has a known vulnerability".replace("  ", " "),
                    category="dependencies",
                    path=report.manager,
                    detail=f"{vuln.id}: {vuln.summary}".strip(": "),
                    remediation=fix,
                )
            )
    return findings, notes
