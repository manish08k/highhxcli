"""SQLite adapter using Python's built-in sqlite3 module (no external tools)."""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
from pathlib import Path

from highhx.core.errors import IntegrationError
from highhx.integrations.databases.base import (
    MIGRATIONS_TABLE,
    DatabaseAdapter,
    PingResult,
    create_table_sql,
    sql_literal,
)
from highhx.utils.time import iso_now


class SQLiteAdapter(DatabaseAdapter):
    kind = "sqlite"
    backup_extension = ".sqlite3"

    @property
    def path(self) -> Path:
        path = self.url.sqlite_path(self.root)
        if path is None:
            raise IntegrationError("An in-memory SQLite database cannot be managed by HighhX.")
        return path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=10)

    def ping(self) -> PingResult:
        try:
            path = self.path
        except IntegrationError as exc:
            return PingResult(False, exc.message)
        if not path.exists():
            return PingResult(False, f"database file {path.name} does not exist yet")
        try:
            with contextlib.closing(self._connect()) as conn:
                version = conn.execute("select sqlite_version()").fetchone()[0]
        except sqlite3.Error as exc:
            return PingResult(False, str(exc))
        return PingResult(True, f"{path.name} is readable", version)

    def execute_script(self, sql: str, *, name: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(f"BEGIN;\n{sql}\n;\nCOMMIT;")
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise IntegrationError(f"SQL error in {name}: {exc}") from exc
        finally:
            conn.close()

    def applied_migrations(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        with contextlib.closing(self._connect()) as conn:
            conn.execute(create_table_sql())
            conn.commit()
            return {row[0]: row[1] for row in conn.execute(f"SELECT name, checksum FROM {MIGRATIONS_TABLE}")}  # nosec B608 - constant table name; values bound as parameters

    def record_migration(self, name: str, checksum: str) -> None:
        with contextlib.closing(self._connect()) as conn:
            conn.execute(create_table_sql())
            conn.execute(
                f"INSERT OR REPLACE INTO {MIGRATIONS_TABLE} (name, checksum, applied_at) VALUES (?, ?, ?)",
                (name, checksum, iso_now()),
            )
            conn.commit()

    def apply_migration(self, name: str, sql: str, checksum: str) -> None:
        """Run the script and record it in one transaction."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(create_table_sql())
            conn.commit()
            conn.executescript(
                "BEGIN;\n"  # nosec B608 - constant table name; values validated by sql_literal
                f"{sql}\n;\n"
                f"INSERT INTO {MIGRATIONS_TABLE} (name, checksum, applied_at) VALUES ({sql_literal(name)}, {sql_literal(checksum, 'checksum')}, '{iso_now()}');\n"
                "COMMIT;"
            )
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise IntegrationError(f"Migration {name} failed: {exc}") from exc
        finally:
            conn.close()

    def backup(self, destination: Path) -> Path:
        if not self.path.exists():
            raise IntegrationError(f"Database file {self.path} does not exist.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connect()) as src, contextlib.closing(sqlite3.connect(str(destination))) as dst:
            src.backup(dst)
        return destination

    def restore(self, source: Path) -> None:
        if not source.is_file():
            raise IntegrationError(f"Backup file {source} does not exist.")
        with contextlib.closing(sqlite3.connect(str(source))) as check:
            try:
                check.execute("PRAGMA schema_version").fetchone()
            except sqlite3.DatabaseError as exc:
                raise IntegrationError(f"{source.name} is not a valid SQLite database: {exc}") from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".restoring")
        shutil.copy2(source, tmp)
        tmp.replace(self.path)
