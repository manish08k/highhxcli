"""Database adapter interface."""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass
from pathlib import Path

from highhx.core.engine import Engine
from highhx.core.errors import ValidationError
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


_SQL_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$")


def sql_literal(value: str, what: str = "migration name") -> str:
    """A SQL string literal for a migration name or checksum.

    Only letters, digits and ``._ -`` are accepted, so the literal needs no escaping and
    means the same in every dialect (MySQL, unlike PostgreSQL and SQLite, also treats
    backslashes as escapes — quote doubling alone is not enough there).
    """
    if not _SQL_SAFE.match(value):
        raise ValidationError(
            f"Unsupported {what} {value!r}.",
            hint="Use letters, digits, '.', '_', '-' and spaces only (e.g. 0003_add_users.sql).",
        )
    return f"'{value}'"
