"""Backup file naming and listing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from highhx.utils.filesystem import human_size
from highhx.utils.time import utc_now


@dataclass
class BackupFile:
    path: Path
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.path.name, "path": str(self.path), "size": self.size, "human_size": human_size(self.size)}


def backup_name(database: str, extension: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", database or "database")
    return f"{safe}-{utc_now().strftime('%Y%m%dT%H%M%SZ')}{extension}"


def list_backups(directory: Path) -> list[BackupFile]:
    if not directory.is_dir():
        return []
    files = [p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")]
    return [BackupFile(p, p.stat().st_size) for p in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)]
