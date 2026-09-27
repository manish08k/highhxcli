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
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS agent_sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            root TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            turns INTEGER NOT NULL DEFAULT 0,
            usage TEXT NOT NULL DEFAULT '{}',
            plan TEXT,
            remote_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_updated ON agent_sessions(updated_at);

        CREATE TABLE IF NOT EXISTS agent_messages (
            session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (session_id, seq)
        );
        """,
    ),
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL,
            session_id TEXT,
            account_id TEXT,
            actor TEXT NOT NULL,
            tool TEXT NOT NULL,
            kind TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            risk TEXT,
            categories TEXT NOT NULL DEFAULT '[]',
            decision TEXT NOT NULL,
            ticket_id TEXT,
            status TEXT NOT NULL,
            verified INTEGER,
            duration REAL,
            error TEXT,
            details TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
        CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
        ALTER TABLE agent_sessions ADD COLUMN account_id TEXT;
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_account ON agent_sessions(account_id, root);
        """,
    ),
    (
        5,
        # One HighhX process at a time may drive a session (host:pid of the holder).
        "ALTER TABLE agent_sessions ADD COLUMN lease_owner TEXT;",
    ),
    (
        6,
        # Plain-language automation runs (deterministic decision, JSON plan, per-step results): `highhx runs`.
        """
        CREATE TABLE IF NOT EXISTS automation_runs (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            request TEXT NOT NULL,
            route TEXT NOT NULL,
            intent TEXT,
            target TEXT,
            risk TEXT,
            executor TEXT,
            status TEXT NOT NULL,
            verification TEXT,
            duration REAL,
            failure TEXT,
            source TEXT,
            decision TEXT NOT NULL DEFAULT '{}',
            steps TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_automation_runs_started ON automation_runs(started_at)
        """,
    ),
)

LATEST_VERSION = MIGRATIONS[-1][0]


def current_version(db: Database) -> int:
    db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = db.query_one("SELECT MAX(version) AS v FROM schema_version")
    return int(row["v"]) if row and row["v"] is not None else 0


def _statements(script: str) -> list[str]:
    """Split a migration script into statements (the scripts contain no literal semicolons)."""
    return [statement.strip() for statement in script.split(";") if statement.strip()]


def apply_migrations(db: Database) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied.

    Several HighhX processes may open a fresh database at the same moment (a workflow and
    `highhx history`, say). Pending migrations are applied inside one ``BEGIN IMMEDIATE``
    transaction and the version is re-read after taking the write lock, so exactly one
    process applies each migration and the others see the result — non-idempotent steps
    such as ``ALTER TABLE … ADD COLUMN`` never run twice.
    """
    if current_version(db) >= LATEST_VERSION:
        return []
    applied: list[int] = []
    with db.immediate_transaction():
        version = current_version(db)
        for number, script in MIGRATIONS:
            if number <= version:
                continue
            for statement in _statements(script):
                db.execute(statement)
            db.execute("INSERT INTO schema_version (version) VALUES (?)", (number,))
            applied.append(number)
    return applied
