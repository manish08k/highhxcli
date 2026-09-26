"""Shallow filesystem scanning for sub-projects."""

from __future__ import annotations

import os
from pathlib import Path

from highhx.utils.filesystem import DEFAULT_IGNORE_DIRS

MANIFEST_FILES = (
    "pyproject.toml",
    "setup.py",
    "package.json",
    "pubspec.yaml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "CMakeLists.txt",
    "Cargo.toml",
    "go.mod",
    "Dockerfile",
)


def find_subprojects(root: Path, *, max_depth: int = 3) -> list[Path]:
    """Directories below ``root`` (excluding root) that contain a manifest file."""
    root = root.resolve()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in DEFAULT_IGNORE_DIRS and not d.startswith("."))
        if depth >= max_depth:
            dirnames[:] = []
        if current == root:
            continue
        if any(name in filenames for name in MANIFEST_FILES):
            found.append(current)
    return found
