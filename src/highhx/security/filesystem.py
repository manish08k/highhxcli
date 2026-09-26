"""Selecting files to scan."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from highhx.utils.filesystem import iter_files, matches_any

BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".tgz",
        ".jar",
        ".war",
        ".class",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".mp4",
        ".mp3",
        ".webp",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".lock",
        ".pyc",
    }
)
ALWAYS_SKIP = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Cargo.lock",
    "pubspec.lock",
    "go.sum",
    "*.min.js",
    "*.map",
)


def scan_candidates(
    root: Path,
    *,
    tracked: Callable[[], list[str]] | None,
    include_untracked: bool,
    ignore: list[str],
    max_size: int,
) -> list[Path]:
    """Files to scan: git-tracked files (committed content), optionally everything else."""
    patterns = [*ALWAYS_SKIP, *ignore]
    paths: list[Path] = []
    if tracked is not None and not include_untracked:
        for rel in tracked():
            if matches_any(rel, patterns):
                continue
            path = root / rel
            if path.is_file() and not path.is_symlink() and path.suffix.lower() not in BINARY_SUFFIXES:
                try:
                    if path.stat().st_size <= max_size:
                        paths.append(path)
                except OSError:
                    continue
        return paths
    for path in iter_files(root, ignore_patterns=patterns, max_size=max_size):
        if path.suffix.lower() not in BINARY_SUFFIXES:
            paths.append(path)
    return paths
