"""File scanning for secrets (values are never reported)."""

from __future__ import annotations

from pathlib import Path

from highhx.security.report import Finding
from highhx.security.secrets import find_secrets
from highhx.utils.filesystem import is_binary_file, read_text

SOURCE_CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".rb",
        ".php",
        ".cs",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".swift",
        ".dart",
        ".scala",
        ".sh",
        ".ps1",
    }
)
SEVERITY_MAP = {"critical": "critical", "high": "high", "medium": "medium", "low": "low"}


def scan_file(root: Path, path: Path) -> list[Finding]:
    if is_binary_file(path):
        return []
    try:
        text = read_text(path)
    except OSError:
        return []
    rel = path.relative_to(root).as_posix()
    findings = []
    lowered = rel.lower()
    in_tests = any(part in lowered for part in ("test", "fixture", "example", "sample", "mock"))
    for match in find_secrets(text, source_code=path.suffix.lower() in SOURCE_CODE_SUFFIXES):
        severity = SEVERITY_MAP.get(match.severity, "medium")
        if in_tests and severity in ("critical", "high"):
            severity = "medium"
        findings.append(
            Finding(
                rule=f"secret:{match.pattern_id}",
                severity=severity,
                title=f"Possible {match.description}",
                category="secrets",
                path=rel,
                line=match.line,
                detail=f"{match.length} character value at column {match.column} (value not shown)",
                remediation="Remove it, rotate the credential, and load it from the environment instead. "
                "If it is a false positive, add `highhx:allow-secret` to the line or allowlist the fingerprint.",
            )
        )
    return findings
