"""Database adapters (SQLite natively; PostgreSQL and MySQL via their CLI tools)."""

from highhx.integrations.databases.base import DatabaseAdapter
from highhx.integrations.databases.url import DatabaseURL, parse_database_url

__all__ = ["DatabaseAdapter", "DatabaseURL", "adapter_for", "parse_database_url"]


def adapter_for(url: DatabaseURL, engine, root):  # type: ignore[no-untyped-def]
    """Create the adapter for ``url``."""
    from highhx.core.errors import IntegrationError

    if url.kind == "sqlite":
        from highhx.integrations.databases.sqlite import SQLiteAdapter

        return SQLiteAdapter(url, engine, root)
    if url.kind == "postgresql":
        from highhx.integrations.databases.postgres import PostgresAdapter

        return PostgresAdapter(url, engine, root)
    if url.kind == "mysql":
        from highhx.integrations.databases.mysql import MySQLAdapter

        return MySQLAdapter(url, engine, root)
    raise IntegrationError(f"Unsupported database type '{url.scheme}'.", hint="Supported: postgresql, mysql, sqlite.")
