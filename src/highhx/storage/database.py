"""Thread-safe SQLite wrapper."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from highhx.utils.filesystem import ensure_dir


class Database:
    """A single SQLite connection guarded by a lock (safe to share between threads)."""

    def __init__(self, path: Path | str) -> None:
        self.path = path
        if isinstance(path, Path):
            ensure_dir(path.parent)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if isinstance(path, Path):
                with contextlib.suppress(sqlite3.DatabaseError):
                    self._conn.execute("PRAGMA journal_mode = WAL")

    @classmethod
    def open(cls, path: Path | str) -> Database:
        """Open (creating if needed) and migrate to the latest schema."""
        from highhx.storage.migrations import apply_migrations

        db = cls(path)
        try:
            apply_migrations(db)
        except BaseException:
            db.close()
            raise
        return db

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
        """Execute a statement; returns the number of affected rows."""
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return cursor.rowcount

    def executescript(self, script: str) -> None:
        with self._lock:
            self._conn.executescript(script)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Run several statements atomically."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @contextlib.contextmanager
    def immediate_transaction(self) -> Iterator[None]:
        """Like :meth:`transaction`, but takes the database write lock up front (other
        processes wait up to the busy timeout instead of racing)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock, contextlib.suppress(sqlite3.Error):
            self._conn.close()
