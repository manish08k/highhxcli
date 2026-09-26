"""Removal of build outputs and caches (never outside the project)."""

from __future__ import annotations

import os
from pathlib import Path

from highhx.utils.filesystem import DEFAULT_IGNORE_DIRS
from highhx.utils.paths import is_within

TOP_LEVEL = (
    "dist",
    "build",
    "target",
    "out",
    ".next",
    "coverage",
    "htmlcov",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".coverage",
    "coverage.xml",
    ".dart_tool/build",
    "builddir",
)
RECURSIVE_DIRS = ("__pycache__",)
RECURSIVE_SUFFIXES = (".egg-info",)


def clean_targets(root: Path, extra: list[str] | None = None) -> list[Path]:
    """Build outputs that can be safely regenerated."""
    root = root.resolve()
    targets: list[Path] = []
    for rel in (*TOP_LEVEL, *(extra or [])):
        path = root / rel
        if path.exists() and not path.is_symlink() and is_within(path, root):
            targets.append(path)
    skip = set(DEFAULT_IGNORE_DIRS) - {"__pycache__"} | {"node_modules", ".venv", "venv", ".git"}
    for dirpath, dirnames, _files in os.walk(root):
        keep = []
        for name in dirnames:
            full = Path(dirpath) / name
            if name in RECURSIVE_DIRS or name.endswith(RECURSIVE_SUFFIXES):
                if full not in targets:
                    targets.append(full)
            elif name not in skip:
                keep.append(name)
        dirnames[:] = keep
    return sorted(set(targets))
