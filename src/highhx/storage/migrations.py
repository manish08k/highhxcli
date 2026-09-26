"""Schema migrations for the HighhX state database."""

from __future__ import annotations

from highhx.storage.database import Database

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS executions (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            command TEXT,
            project TEXT,
            cwd TEXT,
            status TEXT NOT NULL,
            exit_code INTEGER,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            duration REAL,
            error TEXT,
            trace_id TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_exec_started ON executions(started_at);
        CREATE INDEX IF NOT EXISTS idx_exec_kind ON executions(kind, name);

        CREATE TABLE IF NOT EXISTS steps (
            execution_id TEXT NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            step_id TEXT NOT NULL,
            command TEXT,
            status TEXT NOT NULL,
            exit_code INTEGER,
            started_at TEXT,
            duration REAL,
            error TEXT,
            outputs TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (execution_id, seq)
        );

        CREATE TABLE IF NOT EXISTS spans (
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            parent_id TEXT,
            name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            duration REAL,
            status TEXT,
            attributes TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (trace_id, span_id)
        );

        CREATE TABLE IF NOT EXISTS deployments (
            id TEXT PRIMARY KEY,
            target TEXT NOT NULL,
            version TEXT,
            git_sha TEXT,
            strategy TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            execution_id TEXT,
            rollback_of TEXT,
            details TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_deploy_target ON deployments(target, started_at);

        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            expires_at REAL
        );

        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        # The expiring cache table was never used by any feature; drop it.
        "DROP TABLE IF EXISTS cache;",
    ),
)

LATEST_VERSION = MIGRATIONS[-1][0]


def current_version(db: Database) -> int:
    db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = db.query_one("SELECT MAX(version) AS v FROM schema_version")
    return int(row["v"]) if row and row["v"] is not None else 0


def apply_migrations(db: Database) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied."""
    applied: list[int] = []
    version = current_version(db)
    for number, script in MIGRATIONS:
        if number <= version:
            continue
        db.executescript(script)
        db.execute("INSERT INTO schema_version (version) VALUES (?)", (number,))
        applied.append(number)
    return applied
