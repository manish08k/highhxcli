"""Monorepo detection."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.project.manifest import read_json, read_pom, read_toml, read_yaml_mapping
from highhx.project.scanner import find_subprojects
from highhx.utils.filesystem import read_text


@dataclass
class MonorepoInfo:
    tool: str
    members: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "members": self.members, "patterns": self.patterns}


def expand_member_globs(root: Path, patterns: list[str]) -> list[str]:
    """Expand workspace globs (``packages/*``) into existing directories."""
    members: set[str] = set()
    for pattern in patterns:
        pattern = pattern.strip().rstrip("/")
        if not pattern or pattern.startswith("!"):
            continue
        for path in root.glob(pattern):
            if path.is_dir() and "node_modules" not in path.parts:
                members.add(path.relative_to(root).as_posix())
    excluded = {p[1:].rstrip("/") for p in patterns if p.startswith("!")}
    return sorted(m for m in members if m not in excluded)


def detect_monorepo(root: Path) -> MonorepoInfo | None:
    pnpm = read_yaml_mapping(root / "pnpm-workspace.yaml")
    if pnpm is not None:
        patterns = [str(p) for p in pnpm.get("packages") or []]
        return MonorepoInfo("pnpm", expand_member_globs(root, patterns), patterns)
    package = read_json(root / "package.json") or {}
    workspaces = package.get("workspaces")
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    if isinstance(workspaces, list) and workspaces:
        patterns = [str(p) for p in workspaces]
        tool = (
            "nx" if (root / "nx.json").is_file() else "turbo" if (root / "turbo.json").is_file() else "npm-workspaces"
        )
        return MonorepoInfo(tool, expand_member_globs(root, patterns), patterns)
    lerna = read_json(root / "lerna.json")
    if lerna is not None:
        patterns = [str(p) for p in lerna.get("packages") or ["packages/*"]]
        return MonorepoInfo("lerna", expand_member_globs(root, patterns), patterns)
    pyproject = read_toml(root / "pyproject.toml") or {}
    uv_ws = ((pyproject.get("tool") or {}).get("uv") or {}).get("workspace") or {}
    if uv_ws.get("members"):
        patterns = [str(p) for p in uv_ws["members"]]
        return MonorepoInfo("uv-workspace", expand_member_globs(root, patterns), patterns)
    cargo = read_toml(root / "Cargo.toml") or {}
    if (cargo.get("workspace") or {}).get("members"):
        patterns = [str(p) for p in cargo["workspace"]["members"]]
        return MonorepoInfo("cargo-workspace", expand_member_globs(root, patterns), patterns)
    if (root / "go.work").is_file():
        uses = re.findall(r"^\s*(?:use\s+)?\.?/?([\w./-]+)\s*$", read_text(root / "go.work"), re.MULTILINE)
        members = [u for u in uses if (root / u).is_dir() and u not in (".",)]
        return MonorepoInfo("go-workspace", sorted(members))
    pom = read_pom(root / "pom.xml")
    if pom and pom.get("modules"):
        return MonorepoInfo("maven-modules", [m for m in pom["modules"] if (root / m).is_dir()])
    for settings in ("settings.gradle.kts", "settings.gradle"):
        if (root / settings).is_file():
            includes = re.findall(r"include\s*\(?\s*((?:['\"][:\w.-]+['\"]\s*,?\s*)+)", read_text(root / settings))
            names = [
                n.strip(":").replace(":", "/") for group in includes for n in re.findall(r"['\"]([:\w.-]+)['\"]", group)
            ]
            members = [n for n in names if (root / n).is_dir()]
            if len(members) > 1:
                return MonorepoInfo("gradle-multiproject", members)
    grouped = [
        p
        for p in find_subprojects(root, max_depth=2)
        if p.relative_to(root).parts[0] in ("apps", "packages", "services", "libs") and p.parent != root
    ]
    if len(grouped) >= 2:
        return MonorepoInfo("directory-layout", sorted(p.relative_to(root).as_posix() for p in grouped))
    return None
