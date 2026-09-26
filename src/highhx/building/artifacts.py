"""Build artifact discovery and integrity manifest."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from highhx.utils.filesystem import atomic_write_text
from highhx.utils.hashing import sha256_file

ARTIFACT_GLOBS = (
    "dist/*",
    "build/libs/*.jar",
    "build/distributions/*",
    "target/*.jar",
    "target/*.war",
    "build/app/outputs/**/*.apk",
    "build/app/outputs/**/*.aab",
    "build/web/index.html",
    "*.tgz",
    "build/bin/*",
)
MANIFEST_NAME = "artifacts.json"


@dataclass
class Artifact:
    path: str
    size: int
    sha256: str
    modified: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_artifacts(root: Path, patterns: tuple[str, ...] = ARTIFACT_GLOBS) -> list[Artifact]:
    seen: set[Path] = set()
    artifacts: list[Artifact] = []
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path in seen or path.name.startswith("."):
                continue
            seen.add(path)
            stat = path.stat()
            artifacts.append(
                Artifact(
                    path=path.relative_to(root).as_posix(),
                    size=stat.st_size,
                    sha256=sha256_file(path),
                    modified=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds"),
                )
            )
    return artifacts


def write_manifest(state_dir: Path, artifacts: list[Artifact]) -> Path:
    path = state_dir / MANIFEST_NAME
    atomic_write_text(path, json.dumps([a.to_dict() for a in artifacts], indent=2) + "\n")
    return path


def read_manifest(state_dir: Path) -> list[Artifact]:
    path = state_dir / MANIFEST_NAME
    if not path.is_file():
        return []
    return [Artifact(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


def verify(root: Path, state_dir: Path) -> list[dict[str, str]]:
    """Compare artifacts on disk against the recorded manifest."""
    problems = []
    for artifact in read_manifest(state_dir):
        path = root / artifact.path
        if not path.is_file():
            problems.append({"path": artifact.path, "problem": "missing"})
        elif sha256_file(path) != artifact.sha256:
            problems.append({"path": artifact.path, "problem": "checksum mismatch"})
    return problems
