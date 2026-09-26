"""highhx db"""

from __future__ import annotations

import click

from highhx.commands.groups import DefaultGroup


@click.group(
    "db", cls=DefaultGroup, default_command="status", short_help="Database status, migrations, seeds, backups, restore."
)
def db() -> None:
    """Database operations for PostgreSQL, MySQL and SQLite via adapters.
    The connection URL comes from the environment (database.url_env, default DATABASE_URL)."""


def _register() -> None:
    from highhx.commands.database.backup import backup
    from highhx.commands.database.migrate import migrate
    from highhx.commands.database.restore import restore
    from highhx.commands.database.seed import seed
    from highhx.commands.database.status import status

    for command in (status, migrate, seed, backup, restore):
        db.add_command(command)


_register()
