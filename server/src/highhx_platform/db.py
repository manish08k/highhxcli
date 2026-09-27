"""Database engine and session management (SQLAlchemy 2.x; SQLite or PostgreSQL)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str) -> None:
        kwargs: dict[str, object] = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if url in ("sqlite://", "sqlite:///:memory:"):
                kwargs["poolclass"] = StaticPool
        else:
            kwargs["pool_pre_ping"] = True
        self.engine: Engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    def alembic_config(self) -> Config:
        cfg = Config()
        cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        return cfg

    def migrate(self, revision: str = "head") -> None:
        """Apply schema migrations (versioned, with Alembic)."""
        cfg = self.alembic_config()
        with self.engine.begin() as connection:
            cfg.attributes["connection"] = connection
            tables = set(inspect(connection).get_table_names())
            if "users" in tables and "alembic_version" not in tables:
                # Created by HighhX Platform 0.1.0 (create_all, no migration history).
                command.stamp(cfg, "0001")
            command.upgrade(cfg, revision)

    def schema_revision(self) -> str | None:
        with self.engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()

    def head_revision(self) -> str | None:
        return ScriptDirectory.from_config(self.alembic_config()).get_current_head()

    def session(self) -> Iterator[Session]:
        with self.sessions() as session:
            yield session


def _sqlite_pragmas(connection: object, _record: object) -> None:
    cursor = connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.close()
