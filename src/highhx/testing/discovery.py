"""Test framework discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from highhx.config.schema import HighhXConfig
from highhx.project.detector import ProjectProfile
from highhx.project.manifest import read_json

KNOWN = (
    "pytest",
    "unittest",
    "jest",
    "vitest",
    "mocha",
    "npm",
    "flutter",
    "dart",
    "maven",
    "gradle",
    "ctest",
    "go",
    "cargo",
    "make",
)


@dataclass
class TestFramework:
    name: str
    command: str
    source: str
    """Where the command came from: config, detected."""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "command": self.command, "source": self.source}


def infer_framework(command: str) -> str:
    """Guess the framework from a command string (used for parsing output)."""
    lowered = command.lower()
    for name, needle in (
        ("pytest", "pytest"),
        ("unittest", "unittest"),
        ("vitest", "vitest"),
        ("jest", "jest"),
        ("mocha", "mocha"),
        ("flutter", "flutter test"),
        ("dart", "dart test"),
        ("maven", "mvn"),
        ("gradle", "gradle"),
        ("ctest", "ctest"),
        ("go", "go test"),
        ("cargo", "cargo test"),
        ("make", "make"),
    ):
        if needle in lowered:
            return name
    if any(x in lowered for x in ("npm test", "npm run test", "pnpm", "yarn", "bun")):
        return "npm"
    return "custom"


def _node_runner(root: Path) -> str | None:
    package = read_json(root / "package.json") or {}
    script = str((package.get("scripts") or {}).get("test") or "")
    for name in ("vitest", "jest", "mocha"):
        if name in script:
            return name
    deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
    for name in ("vitest", "jest", "mocha"):
        if name in deps:
            return name
    return None


def discover(root: Path, profile: ProjectProfile, config: HighhXConfig) -> TestFramework | None:
    configured = config.commands.get("test")
    if configured:
        name = infer_framework(configured)
        if name == "npm":
            name = _node_runner(root) or "npm"
        return TestFramework(name, configured, "config")
    detected = profile.commands.get("test")
    if not detected:
        return None
    name = infer_framework(detected)
    if name == "npm":
        name = _node_runner(root) or "npm"
    return TestFramework(name, detected, "detected")
