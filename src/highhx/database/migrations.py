"""SQL file migrations (``NNN_name.sql``) with checksum tracking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from highhx.utils.hashing import sha256_text

MIGRATION_RE = re.compile(r"^(\d+)[_-].+\.sql$")


@dataclass
class Migration:
    name: str
    path: Path
    checksum: str

    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


@dataclass
class MigrationStatus:
    applied: list[str]
    pending: list[Migration]
    changed: list[str]
    unknown: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "pending": [m.name for m in self.pending],
            "changed": self.changed,
            "unknown": self.unknown,
        }


def discover(directory: Path) -> list[Migration]:
    if not directory.is_dir():
        return []
    files = [p for p in directory.iterdir() if p.is_file() and MIGRATION_RE.match(p.name)]
    files.sort(key=lambda p: (int(MIGRATION_RE.match(p.name).group(1)), p.name))  # type: ignore[union-attr]
    return [Migration(p.stem, p, sha256_text(p.read_text(encoding="utf-8"))) for p in files]


def compare(migrations: list[Migration], applied: dict[str, str]) -> MigrationStatus:
    known = {m.name for m in migrations}
    return MigrationStatus(
        applied=[m.name for m in migrations if m.name in applied],
        pending=[m for m in migrations if m.name not in applied],
        changed=[m.name for m in migrations if m.name in applied and applied[m.name] != m.checksum],
        unknown=sorted(set(applied) - known),
    )


def default_directory(root: Path, configured: str | None) -> Path:
    if configured:
        return root / configured
    for candidate in ("migrations", "db/migrations", "database/migrations", "sql/migrations"):
        if (root / candidate).is_dir():
            return root / candidate
    return root / "migrations"
