"""Semantic versions and version files."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path

from highhx.core.errors import ValidationError
from highhx.utils.filesystem import atomic_write_text, read_text

SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
BUMPS = ("major", "minor", "patch", "prerelease")


@total_ordering
@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: str | None = None
    build: str | None = None

    @classmethod
    def parse(cls, text: str) -> SemVer:
        match = SEMVER_RE.match(text.strip().lstrip("v"))
        if not match:
            raise ValueError(f"not a semantic version: {text!r}")
        return cls(int(match["major"]), int(match["minor"]), int(match["patch"]), match["pre"], match["build"])

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += f"-{self.prerelease}"
        if self.build:
            text += f"+{self.build}"
        return text

    def _key(self) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
        pre: tuple[tuple[int, int | str], ...] = ()
        if self.prerelease:
            pre = tuple((0, int(p)) if p.isdigit() else (1, p) for p in self.prerelease.split("."))
        return (self.major, self.minor, self.patch, 0 if self.prerelease else 1, pre)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._key() < other._key()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SemVer) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def bump(self, part: str, *, pre_id: str = "rc") -> SemVer:
        if part == "major":
            return SemVer(self.major + 1, 0, 0)
        if part == "minor":
            return SemVer(self.major, self.minor + 1, 0)
        if part == "patch":
            if self.prerelease:
                return SemVer(self.major, self.minor, self.patch)
            return SemVer(self.major, self.minor, self.patch + 1)
        if part == "prerelease":
            if self.prerelease:
                parts = self.prerelease.split(".")
                if parts[-1].isdigit():
                    parts[-1] = str(int(parts[-1]) + 1)
                else:
                    parts.append("1")
                return SemVer(self.major, self.minor, self.patch, ".".join(parts))
            return SemVer(self.major, self.minor, self.patch + 1, f"{pre_id}.1")
        raise ValueError(f"unknown bump '{part}' (expected {', '.join(BUMPS)})")


@dataclass
class VersionFile:
    """A file that stores the project version and knows how to rewrite it."""

    path: Path
    kind: str

    def read(self) -> str | None:
        if not self.path.is_file():
            return None
        text = read_text(self.path)
        match = _pattern(self.kind).search(text)
        return match.group("version") if match else None

    def write(self, version: str) -> bool:
        text = read_text(self.path)
        pattern = _pattern(self.kind)
        match = pattern.search(text)
        if not match:
            return False
        start, end = match.span("version")
        new_text = text[:start] + version + text[end:]
        if self.kind == "pubspec":
            new_text = text[:start] + version + text[end:]
        atomic_write_text(self.path, new_text)
        return True


def _pattern(kind: str) -> re.Pattern[str]:
    if kind == "pyproject":
        return re.compile(r"(?ms)^\[(?:project|tool\.poetry)\][^\[]*?^version\s*=\s*[\"'](?P<version>[^\"']+)[\"']")
    if kind == "package.json":
        return re.compile(r"(?m)^\s{0,4}\"version\"\s*:\s*\"(?P<version>[^\"]+)\"")
    if kind == "pubspec":
        return re.compile(r"(?m)^version:\s*['\"]?(?P<version>[0-9][^\s'\"+]*)")
    if kind == "python-init":
        return re.compile(r"(?m)^__version__\s*=\s*[\"'](?P<version>[^\"']+)[\"']")
    if kind == "gradle":
        return re.compile(r"(?m)^\s*version\s*=?\s*[\"'](?P<version>[^\"']+)[\"']")
    if kind == "cargo":
        return re.compile(r"(?ms)^\[package\][^\[]*?^version\s*=\s*\"(?P<version>[^\"]+)\"")
    if kind == "plain":
        return re.compile(r"^\s*(?P<version>\d+\.\d+\.\d+[^\s]*)")
    raise ValueError(f"unknown version file kind {kind}")


def _kind_for(path: Path) -> str:
    name = path.name
    if name == "pyproject.toml":
        return "pyproject"
    if name == "package.json":
        return "package.json"
    if name == "pubspec.yaml":
        return "pubspec"
    if name == "Cargo.toml":
        return "cargo"
    if name in ("build.gradle", "build.gradle.kts"):
        return "gradle"
    if name.endswith(".py"):
        return "python-init"
    return "plain"


def discover_version_files(root: Path, configured: list[str] | None = None) -> list[VersionFile]:
    if configured:
        return [VersionFile(root / rel, _kind_for(root / rel)) for rel in configured]
    candidates = [
        root / n
        for n in (
            "pyproject.toml",
            "package.json",
            "pubspec.yaml",
            "Cargo.toml",
            "build.gradle.kts",
            "build.gradle",
            "VERSION",
        )
    ]
    files = [VersionFile(p, _kind_for(p)) for p in candidates if p.is_file()]
    files = [f for f in files if f.read() is not None]
    # pyproject with dynamic version: look for __version__ in src packages.
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and not any(f.kind == "pyproject" for f in files):
        for init in sorted(root.glob("src/*/__init__.py")) + sorted(root.glob("*/__init__.py")):
            vf = VersionFile(init, "python-init")
            if vf.read():
                files.append(vf)
                break
    return files


def current_version(files: list[VersionFile]) -> tuple[SemVer | None, list[str]]:
    """Version from files; returns problems if files disagree or are unparsable."""
    problems: list[str] = []
    versions: dict[str, SemVer] = {}
    for vf in files:
        raw = vf.read()
        if raw is None:
            continue
        try:
            versions[vf.path.name] = SemVer.parse(raw)
        except ValueError:
            problems.append(f"{vf.path.name}: '{raw}' is not a semantic version")
    distinct = set(versions.values())
    if len(distinct) > 1:
        problems.append("version files disagree: " + ", ".join(f"{k}={v}" for k, v in versions.items()))
    if not versions:
        return None, problems
    return max(distinct), problems


def write_version(files: list[VersionFile], version: SemVer) -> list[str]:
    updated = []
    for vf in files:
        if vf.kind == "package.json":
            data = json.loads(read_text(vf.path))
            if "version" not in data:
                continue
        if vf.write(str(version)):
            updated.append(vf.path.name)
    if not updated:
        raise ValidationError("No version file could be updated.")
    return updated


def suggest_bump(commits: Sequence[object]) -> str:
    """major / minor / patch from Conventional Commits (defaults to patch)."""
    bump = "patch"
    for commit in commits:
        cc = getattr(commit, "conventional", None)
        if cc is None:
            continue
        if cc.breaking:
            return "major"
        if cc.type == "feat":
            bump = "minor"
    return bump
