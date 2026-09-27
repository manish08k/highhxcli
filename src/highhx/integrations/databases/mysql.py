"""MySQL / MariaDB adapter using the mysql and mysqldump clients. The password is passed via MYSQL_PWD."""

from __future__ import annotations

from pathlib import Path

from highhx.core.errors import IntegrationError, ToolNotFoundError
from highhx.execution.command import CommandSpec
from highhx.integrations.databases.base import (
    MIGRATIONS_TABLE,
    DatabaseAdapter,
    PingResult,
    create_table_sql,
    sql_literal,
)
from highhx.utils.time import iso_now


class MySQLAdapter(DatabaseAdapter):
    kind = "mysql"
    backup_extension = ".sql"
    required_tools = ("mysql", "mysqldump")

    def _env(self) -> dict[str, str]:
        return {"MYSQL_PWD": self.url.password} if self.url.password else {}

    def _conn_args(self) -> list[str]:
        args = ["--protocol=TCP"] if self.url.host else []
        if self.url.host:
            args += ["-h", self.url.host]
        if self.url.port:
            args += ["-P", str(self.url.port)]
        if self.url.user:
            args += ["-u", self.url.user]
        return args

    def _require(self, tool: str) -> None:
        from highhx.utils.processes import which

        if which(tool) is None:
            raise ToolNotFoundError(tool, purpose="manage the MySQL database")

    def _mysql(self, *args: str, stdin: str | None = None, name: str = "mysql") -> CommandSpec:
        self._require("mysql")
        database = [self.url.database] if self.url.database else []
        return CommandSpec(
            ["mysql", *self._conn_args(), *args, *database], env=self._env(), stdin_data=stdin, name=name, timeout=3600
        )

    def ping(self) -> PingResult:
        try:
            result = self.engine.capture(self._mysql("-N", "-B", "-e", "SELECT VERSION()", name="mysql-ping"))
        except ToolNotFoundError as exc:
            return PingResult(False, exc.message)
        if result.ok:
            return PingResult(True, f"connected to {self.url.masked()}", result.stdout.strip() or None)
        return PingResult(False, (result.stderr or result.error or "connection failed").strip().splitlines()[-1])

    def execute_script(self, sql: str, *, name: str) -> None:
        result = self.engine.run(self._mysql(stdin=sql, name=f"sql:{name}"), approved=True, record=False)
        if not result.ok:
            raise IntegrationError(f"SQL script {name} failed", details=result.stderr.strip().splitlines()[-5:])

    def applied_migrations(self) -> dict[str, str]:
        self.execute_script(create_table_sql().replace('"', "`"), name="create-migrations-table")
        result = self.engine.capture(self._mysql("-N", "-B", "-e", f"SELECT name, checksum FROM {MIGRATIONS_TABLE}"))  # nosec B608 - constant table name; values validated by sql_literal
        if not result.ok:
            raise IntegrationError("Could not read applied migrations", details=result.stderr.strip().splitlines()[-3:])
        rows = [line.split("\t", 1) for line in result.stdout.splitlines() if "\t" in line]
        return dict(rows)

    def record_migration(self, name: str, checksum: str) -> None:
        self.execute_script(
            f"REPLACE INTO {MIGRATIONS_TABLE} (name, checksum, applied_at) VALUES ({sql_literal(name)}, {sql_literal(checksum, 'checksum')}, '{iso_now()}');",
            name="record-migration",
        )

    def backup(self, destination: Path) -> Path:
        self._require("mysqldump")
        if not self.url.database:
            raise IntegrationError("The database URL does not name a database to back up.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        spec = CommandSpec(
            [
                "mysqldump",
                *self._conn_args(),
                "--single-transaction",
                # Without this, mysqldump needs the global PROCESS privilege that
                # application users normally do not have.
                "--no-tablespaces",
                "--routines",
                "--triggers",
                f"--result-file={destination}",
                self.url.database,
            ],
            env=self._env(),
            name="mysqldump",
            timeout=6 * 3600,
        )
        result = self.engine.run(spec, approved=True)
        if not result.ok and not result.dry_run:
            raise IntegrationError("mysqldump failed", details=result.stderr.strip().splitlines()[-5:])
        return destination

    def restore(self, source: Path) -> None:
        spec = self._mysql("-e", f"source {source.as_posix()}", name="mysql-restore")
        result = self.engine.run(spec, approved=True)
        if not result.ok and not result.dry_run:
            raise IntegrationError("MySQL restore failed", details=result.stderr.strip().splitlines()[-5:])
