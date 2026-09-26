"""Restore source resolution."""

from __future__ import annotations

from pathlib import Path

from highhx.core.errors import NotFoundError
from highhx.database.backup import list_backups


def resolve_backup(directory: Path, name: str | None) -> Path:
    """``name`` may be a path, a file name in the backups directory, or None (latest)."""
    if name:
        candidate = Path(name)
        if candidate.is_file():
            return candidate
        in_dir = directory / name
        if in_dir.is_file():
            return in_dir
        raise NotFoundError(f"Backup '{name}' not found.", hint="List backups with `highhx db backup --list`.")
    backups = list_backups(directory)
    if not backups:
        raise NotFoundError("No backups found.", hint="Create one with `highhx db backup`.")
    return backups[0].path
