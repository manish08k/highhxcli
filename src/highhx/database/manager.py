"""Database management service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.config.schema import DatabaseConfig
from highhx.core.engine import Engine
from highhx.core.errors import ConfigError, IntegrationError, ValidationError
from highhx.database import migrations as mig
from highhx.database.backup import BackupFile, backup_name, list_backups
from highhx.database.restore import resolve_backup
from highhx.database.seeding import seed_files
from highhx.execution.command import CommandSpec
from highhx.integrations.databases import DatabaseAdapter, adapter_for, parse_database_url
from highhx.integrations.databases.url import DatabaseURL


@dataclass
class DatabaseStatus:
    url: str
    kind: str
    reachable: bool
    message: str
    server_version: str | None = None
    migrations: dict[str, Any] | None = None
    missing_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class DatabaseManager:
    def __init__(
        self,
        engine: Engine,
        root: Path,
        config: DatabaseConfig,
        env: dict[str, str],
        *,
        profile: str | None = None,
        protected: bool = False,
    ) -> None:
        self.engine = engine
        self.root = root
        self.config = config
        self.env = env
        self.profile = profile
        self.protected = protected

    def url(self) -> DatabaseURL:
        raw = self.env.get(self.config.url_env) or os.environ.get(self.config.url_env) or self.config.url
        if not raw:
            raise ConfigError(
                f"{self.config.url_env} is not set.",
                hint=f"Set it with `highhx env set {self.config.url_env}` (e.g. sqlite:///app.db or postgresql://…).",
            )
        return parse_database_url(raw)

    def adapter(self) -> DatabaseAdapter:
        return adapter_for(self.url(), self.engine, self.root)

    def _risk(self, base: RiskLevel) -> RiskLevel:
        return RiskLevel.CRITICAL if self.protected else base

    @property
    def backups_dir(self) -> Path:
        return self.root / self.config.backups_dir

    def migrations_dir(self) -> Path:
        return mig.default_directory(self.root, self.config.migrations_dir)

    def status(self) -> DatabaseStatus:
        url = self.url()
        adapter = self.adapter()
        ping = adapter.ping()
        status = DatabaseStatus(
            url.masked(), url.kind, ping.ok, ping.message, ping.server_version, missing_tools=adapter.missing_tools()
        )
        if self.config.migrations_command:
            status.migrations = {"managed_by": self.config.migrations_command}
        elif ping.ok:
            files = mig.discover(self.migrations_dir())
            try:
                status.migrations = mig.compare(files, adapter.applied_migrations()).to_dict()
            except IntegrationError as exc:
                status.migrations = {"error": exc.message}
        return status

    def migrate(self) -> list[str]:
        """Apply pending migrations. Returns names applied (or to be applied in dry-run)."""
        target = self.url().masked()
        if self.config.migrations_command:
            self.engine.approve(
                f"Run migrations on {target}: {self.config.migrations_command}",
                self._risk(RiskLevel.DANGEROUS),
                policy_action="db:migrate",
            )
            result = self.engine.run(
                CommandSpec(self.config.migrations_command, cwd=self.root, env=self.env, name="migrate"), approved=True
            )
            self.engine.raise_for(result)
            return [self.config.migrations_command]
        adapter = self.adapter()
        files = mig.discover(self.migrations_dir())
        if not files:
            raise ValidationError(
                f"No migrations found in {self.migrations_dir().relative_to(self.root).as_posix()}/.",
                hint="Add NNN_description.sql files or set database.migrations.command.",
            )
        state = mig.compare(files, adapter.applied_migrations())
        if state.changed:
            raise ValidationError(
                "Applied migrations were modified after being applied",
                details=state.changed,
                hint="Create a new migration instead of editing an applied one.",
            )
        if not state.pending:
            return []
        self.engine.approve(
            f"Apply {len(state.pending)} migration(s) to {target}",
            self._risk(RiskLevel.DANGEROUS),
            details=[m.name for m in state.pending],
            policy_action="db:migrate",
        )
        if self.engine.dry_run:
            return [m.name for m in state.pending]
        applied = []
        with self.engine.operation("db", "migrate", metadata={"pending": len(state.pending)}):
            for migration in state.pending:
                adapter.apply_migration(migration.name, migration.sql(), migration.checksum)
                applied.append(migration.name)
                self.engine.output.info(f"applied {migration.name}")
        return applied

    def seed(self) -> list[str]:
        target = self.url().masked()
        if self.config.seed_command:
            self.engine.approve(
                f"Seed {target}: {self.config.seed_command}", self._risk(RiskLevel.DANGEROUS), policy_action="db:seed"
            )
            self.engine.raise_for(
                self.engine.run(
                    CommandSpec(self.config.seed_command, cwd=self.root, env=self.env, name="seed"), approved=True
                )
            )
            return [self.config.seed_command]
        files = seed_files(self.root, self.config.seed_dir)
        if not files:
            raise ValidationError(
                "No seed files found (seeds/*.sql).", hint="Add SQL seed files or set database.seed.command."
            )
        self.engine.approve(
            f"Load {len(files)} seed file(s) into {target}",
            self._risk(RiskLevel.DANGEROUS),
            details=[f.name for f in files],
            policy_action="db:seed",
        )
        if self.engine.dry_run:
            return [f.name for f in files]
        adapter = self.adapter()
        with self.engine.operation("db", "seed"):
            for path in files:
                adapter.execute_script(path.read_text(encoding="utf-8"), name=path.name)
        return [f.name for f in files]

    def backup(self) -> Path:
        url = self.url()
        adapter = self.adapter()
        name = url.database or (Path(url.path).stem if url.path else "database")
        destination = self.backups_dir / backup_name(name, adapter.backup_extension)
        self.engine.approve(
            f"Back up {url.masked()} to {destination.relative_to(self.root).as_posix()}",
            RiskLevel.NORMAL,
            policy_action="db:backup",
        )
        if self.engine.dry_run:
            return destination
        with self.engine.operation("db", "backup"):
            return adapter.backup(destination)

    def backups(self) -> list[BackupFile]:
        return list_backups(self.backups_dir)

    def restore(self, name: str | None = None, *, safety_backup: bool = True) -> tuple[Path, Path | None]:
        """Restore from a backup; takes a safety backup of the current data first."""
        url = self.url()
        source = resolve_backup(self.backups_dir, name)
        adapter = self.adapter()
        self.engine.approve(
            f"RESTORE {url.masked()} from {source.name} — current data will be overwritten",
            RiskLevel.CRITICAL,
            details=["A safety backup is taken first." if safety_backup else "No safety backup will be taken."],
            confirm_word=url.database or "restore",
            policy_action="db:restore",
        )
        if self.engine.dry_run:
            return source, None
        with self.engine.operation("db", "restore", metadata={"source": source.name}):
            safety: Path | None = None
            if safety_backup and adapter.ping().ok:
                safety = adapter.backup(
                    self.backups_dir / backup_name(f"pre-restore-{url.database or 'db'}", adapter.backup_extension)
                )
            adapter.restore(source)
        return source, safety
