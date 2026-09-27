"""Alembic environment. The connection is provided by highhx_platform.db.Database."""

from __future__ import annotations

from alembic import context

from highhx_platform import models  # noqa: F401 - registers tables
from highhx_platform.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        raise RuntimeError("Run migrations through highhx_platform.db.Database.migrate().")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
