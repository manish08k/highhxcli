"""Parsers for local vulnerability audit tools (pip-audit, npm/pnpm/yarn audit, cargo audit)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITY_ORDER = ("critical", "high", "moderate", "medium", "low", "info", "unknown")


@dataclass
class Vulnerability:
    package: str
    version: str | None
    id: str
    severity: str = "unknown"
    fix_versions: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sev(value: Any) -> str:
    text = str(value or "unknown").lower()
    return text if text in SEVERITY_ORDER else "unknown"


def parse_audit(fmt: str, stdout: str) -> list[Vulnerability]:
    text = stdout.strip()
    if not text:
        return []
    if fmt == "pip-audit":
        data = json.loads(text)
        deps = data.get("dependencies", data) if isinstance(data, dict) else data
        vulns = []
        for dep in deps:
            for v in dep.get("vulns", []):
                vulns.append(
                    Vulnerability(
                        dep.get("name", "?"),
                        dep.get("version"),
                        v.get("id", "?"),
                        "unknown",
                        list(v.get("fix_versions") or []),
                        (v.get("description") or "")[:200],
                    )
                )
        return vulns
    if fmt == "npm-audit":
        data = json.loads(text)
        vulns = []
        if "vulnerabilities" in data:  # npm >= 7
            for name, info in data["vulnerabilities"].items():
                via = [v for v in info.get("via", []) if isinstance(v, dict)]
                ids = [str(v.get("url") or v.get("source") or v.get("title")) for v in via] or ["transitive"]
                fix = info.get("fixAvailable")
                fix_versions = [fix["version"]] if isinstance(fix, dict) and fix.get("version") else []
                vulns.append(
                    Vulnerability(
                        name,
                        info.get("range"),
                        ", ".join(ids[:2]),
                        _sev(info.get("severity")),
                        fix_versions,
                        (via[0].get("title") if via else "") or "",
                    )
                )
        elif "advisories" in data:  # npm 6 / pnpm
            for adv in data["advisories"].values():
                vulns.append(
                    Vulnerability(
                        adv.get("module_name", "?"),
                        None,
                        str(adv.get("url") or adv.get("id")),
                        _sev(adv.get("severity")),
                        [adv["patched_versions"]] if adv.get("patched_versions") else [],
                        adv.get("title", ""),
                    )
                )
        return vulns
    if fmt == "yarn-audit":
        vulns = []
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "auditAdvisory":
                adv = obj["data"]["advisory"]
                vulns.append(
                    Vulnerability(
                        adv.get("module_name", "?"),
                        None,
                        str(adv.get("url") or adv.get("id")),
                        _sev(adv.get("severity")),
                        [adv["patched_versions"]] if adv.get("patched_versions") else [],
                        adv.get("title", ""),
                    )
                )
        return vulns
    if fmt == "cargo-audit":
        data = json.loads(text)
        vulns = []
        for item in (data.get("vulnerabilities") or {}).get("list", []):
            adv, pkg = item.get("advisory", {}), item.get("package", {})
            vulns.append(
                Vulnerability(
                    pkg.get("name", "?"),
                    pkg.get("version"),
                    adv.get("id", "?"),
                    _sev(adv.get("severity")),
                    list((item.get("versions") or {}).get("patched") or []),
                    adv.get("title", ""),
                )
            )
        return vulns
    raise ValueError(f"unknown audit format {fmt}")


def severity_counts(vulns: list[Vulnerability]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for vuln in vulns:
        counts[vuln.severity] = counts.get(vuln.severity, 0) + 1
    return counts
