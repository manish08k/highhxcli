"""Security findings and report rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from highhx.utils.hashing import short_hash
from highhx.utils.time import iso_now

SEVERITIES = ("critical", "high", "medium", "low", "info")


@dataclass
class Finding:
    """A concrete, locatable security finding. Never contains secret values."""

    rule: str
    severity: str
    title: str
    category: str
    path: str | None = None
    line: int | None = None
    detail: str = ""
    remediation: str = ""

    @property
    def fingerprint(self) -> str:
        return short_hash(f"{self.rule}|{self.path}|{self.line}|{self.title}")

    @property
    def location(self) -> str:
        if self.path and self.line:
            return f"{self.path}:{self.line}"
        return self.path or "-"

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "rule": self.rule,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "path": self.path,
            "line": self.line,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass
class SecurityReport:
    findings: list[Finding] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    files_scanned: int = 0
    suppressed: int = 0
    generated_at: str = field(default_factory=iso_now)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def sorted(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (
                SEVERITIES.index(f.severity) if f.severity in SEVERITIES else 9,
                f.category,
                f.path or "",
                f.line or 0,
            ),
        )

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(SEVERITIES, 0)
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    def at_least(self, threshold: str) -> list[Finding]:
        limit = SEVERITIES.index(threshold)
        return [f for f in self.findings if f.severity in SEVERITIES and SEVERITIES.index(f.severity) <= limit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "files_scanned": self.files_scanned,
            "checks": self.checks_run,
            "counts": self.counts(),
            "suppressed": self.suppressed,
            "notes": self.notes,
            "findings": [f.to_dict() for f in self.sorted()],
        }

    def to_markdown(self, project: str | None = None) -> str:
        lines = [
            f"# Security report{f' — {project}' if project else ''}",
            "",
            f"Generated {self.generated_at} by HighhX (local checks only).",
            "",
        ]
        lines.append(
            "This report lists concrete findings from the checks that ran. An empty report does not mean the project is secure."
        )
        lines += ["", "## Summary", "", "| Severity | Count |", "|---|---|"]
        lines += [f"| {sev} | {count} |" for sev, count in self.counts().items()]
        lines += ["", f"Files scanned: {self.files_scanned}. Checks: {', '.join(self.checks_run)}.", ""]
        if self.notes:
            lines += ["## Notes", ""] + [f"- {n}" for n in self.notes] + [""]
        lines += ["## Findings", ""]
        if not self.findings:
            lines.append("No findings.")
        for f in self.sorted():
            lines += [
                f"### [{f.severity.upper()}] {f.title}",
                "",
                f"- Rule: `{f.rule}` ({f.category})",
                f"- Location: `{f.location}`",
                f"- Fingerprint: `{f.fingerprint}`",
            ]
            if f.detail:
                lines.append(f"- Detail: {f.detail}")
            if f.remediation:
                lines.append(f"- Remediation: {f.remediation}")
            lines.append("")
        return "\n".join(lines) + "\n"
