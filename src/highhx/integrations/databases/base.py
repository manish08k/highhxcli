"""Database adapter interface."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path

from highhx.core.engine import Engine
from highhx.integrations.databases.url import DatabaseURL

MIGRATIONS_TABLE = "highhx_migrations"


@dataclass
class PingResult:
    ok: bool
    message: str
    server_version: str | None = None


class DatabaseAdapter(abc.ABC):
    """Operations HighhX needs from a database. Approvals are handled by the caller."""

    kind: str = "abstract"
    backup_extension: str = ".sql"
    required_tools: tuple[str, ...] = ()

    def __init__(self, url: DatabaseURL, engine: Engine, root: Path) -> None:
        self.url = url
        self.engine = engine
        self.root = root

    def missing_tools(self) -> list[str]:
        from highhx.utils.processes import which

        return [tool for tool in self.required_tools if which(tool) is None]

    @abc.abstractmethod
    def ping(self) -> PingResult:
        """Check connectivity."""

    @abc.abstractmethod
    def execute_script(self, sql: str, *, name: str) -> None:
        """Execute a SQL script atomically where the database allows it. Raises on failure."""

    @abc.abstractmethod
    def applied_migrations(self) -> dict[str, str]:
        """Map of applied migration name → checksum (creates the tracking table if needed)."""

    @abc.abstractmethod
    def record_migration(self, name: str, checksum: str) -> None:
        """Record a migration as applied."""

    @abc.abstractmethod
    def backup(self, destination: Path) -> Path:
        """Write a backup to ``destination`` and return the file written."""

    @abc.abstractmethod
    def restore(self, source: Path) -> None:
        """Restore the database from ``source`` (destructive)."""

    def apply_migration(self, name: str, sql: str, checksum: str) -> None:
        """Run a migration script and record it."""
        self.execute_script(sql, name=name)
        self.record_migration(name, checksum)


def create_table_sql(quote: str = '"') -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} ("
        f"name VARCHAR(255) PRIMARY KEY, checksum VARCHAR(64) NOT NULL, applied_at VARCHAR(40) NOT NULL)"
    )
