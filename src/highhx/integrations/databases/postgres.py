"""PostgreSQL adapter using psql / pg_dump / pg_restore. The password is passed via PGPASSWORD."""

from __future__ import annotations

from pathlib import Path

from highhx.core.errors import IntegrationError, ToolNotFoundError
from highhx.execution.command import CommandSpec
from highhx.integrations.databases.base import MIGRATIONS_TABLE, DatabaseAdapter, PingResult, create_table_sql
from highhx.utils.time import iso_now


class PostgresAdapter(DatabaseAdapter):
    kind = "postgresql"
    backup_extension = ".dump"
    required_tools = ("psql", "pg_dump", "pg_restore")

    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.url.password:
            env["PGPASSWORD"] = self.url.password
        for key, value in self.url.params:
            if key == "sslmode":
                env["PGSSLMODE"] = value
        return env

    def _conn_args(self) -> list[str]:
        args: list[str] = []
        if self.url.host:
            args += ["-h", self.url.host]
        if self.url.port:
            args += ["-p", str(self.url.port)]
        if self.url.user:
            args += ["-U", self.url.user]
        return args

    def _require(self, tool: str) -> None:
        from highhx.utils.processes import which

        if which(tool) is None:
            raise ToolNotFoundError(tool, purpose="manage the PostgreSQL database")

    def _psql(self, *args: str, stdin: str | None = None, name: str = "psql") -> CommandSpec:
        self._require("psql")
        return CommandSpec(
            ["psql", "-X", "-v", "ON_ERROR_STOP=1", *self._conn_args(), "-d", self.url.database or "postgres", *args],
            env=self._env(),
            stdin_data=stdin,
            name=name,
            timeout=3600,
        )

    def ping(self) -> PingResult:
        try:
            result = self.engine.capture(self._psql("-At", "-c", "SHOW server_version", name="psql-ping"))
        except ToolNotFoundError as exc:
            return PingResult(False, exc.message)
        if result.ok:
            return PingResult(True, f"connected to {self.url.masked()}", result.stdout.strip() or None)
        return PingResult(False, (result.stderr or result.error or "connection failed").strip().splitlines()[-1])

    def execute_script(self, sql: str, *, name: str) -> None:
        result = self.engine.run(
            self._psql("--single-transaction", "-f", "-", stdin=sql, name=f"sql:{name}"), approved=True, record=False
        )
        if not result.ok:
            raise IntegrationError(f"SQL script {name} failed", details=result.stderr.strip().splitlines()[-5:])

    def applied_migrations(self) -> dict[str, str]:
        self.execute_script(create_table_sql(), name="create-migrations-table")
        result = self.engine.capture(
            self._psql("-At", "-F", "\t", "-c", f"SELECT name, checksum FROM {MIGRATIONS_TABLE}")
        )
        if not result.ok:
            raise IntegrationError("Could not read applied migrations", details=result.stderr.strip().splitlines()[-3:])
        rows = [line.split("\t", 1) for line in result.stdout.splitlines() if "\t" in line]
        return dict(rows)

    def record_migration(self, name: str, checksum: str) -> None:
        escaped = name.replace("'", "''")
        self.execute_script(
            f"INSERT INTO {MIGRATIONS_TABLE} (name, checksum, applied_at) VALUES ('{escaped}', '{checksum}', '{iso_now()}') "
            f"ON CONFLICT (name) DO UPDATE SET checksum = EXCLUDED.checksum;",
            name="record-migration",
        )

    def apply_migration(self, name: str, sql: str, checksum: str) -> None:
        escaped = name.replace("'", "''")
        script = (
            f"{sql}\n;\nINSERT INTO {MIGRATIONS_TABLE} (name, checksum, applied_at) "
            f"VALUES ('{escaped}', '{checksum}', '{iso_now()}');"
        )
        self.execute_script(script, name=name)

    def backup(self, destination: Path) -> Path:
        self._require("pg_dump")
        destination.parent.mkdir(parents=True, exist_ok=True)
        spec = CommandSpec(
            ["pg_dump", "-Fc", *self._conn_args(), "-f", str(destination), self.url.database or "postgres"],
            env=self._env(),
            name="pg_dump",
            timeout=6 * 3600,
        )
        result = self.engine.run(spec, approved=True)
        if not result.ok and not result.dry_run:
            raise IntegrationError("pg_dump failed", details=result.stderr.strip().splitlines()[-5:])
        return destination

    def restore(self, source: Path) -> None:
        self._require("pg_restore")
        spec = CommandSpec(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                *self._conn_args(),
                "-d",
                self.url.database or "postgres",
                str(source),
            ],
            env=self._env(),
            name="pg_restore",
            timeout=6 * 3600,
        )
        result = self.engine.run(spec, approved=True)
        if not result.ok and not result.dry_run:
            raise IntegrationError("pg_restore failed", details=result.stderr.strip().splitlines()[-5:])
