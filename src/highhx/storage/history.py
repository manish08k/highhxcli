"""Execution history."""

from __future__ import annotations

import builtins
import json
import os
from dataclasses import dataclass, field
from typing import Any

from highhx.core.errors import NotFoundError
from highhx.security.secrets import Redactor
from highhx.storage.database import Database
from highhx.utils.hashing import new_id
from highhx.utils.time import iso_now


@dataclass
class StepRecord:
    """A recorded step (workflow step or sub-command of an operation)."""

    step_id: str
    status: str
    command: str | None = None
    exit_code: int | None = None
    started_at: str | None = None
    duration: float | None = None
    error: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ExecutionRecord:
    """A recorded HighhX execution."""

    id: str
    kind: str
    name: str
    status: str
    started_at: str
    command: str | None = None
    project: str | None = None
    cwd: str | None = None
    exit_code: int | None = None
    finished_at: str | None = None
    duration: float | None = None
    error: str | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    steps: list[StepRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "steps"}
        data["steps"] = [s.to_dict() for s in self.steps]
        return data


class HistoryStore:
    """Persists executions and their steps. All text is redacted before storage."""

    def __init__(self, db: Database, redactor: Redactor | None = None) -> None:
        self.db = db
        self.redactor = redactor or Redactor()

    def _r(self, text: str | None) -> str | None:
        return self.redactor.redact(text) if text else text

    def start(
        self,
        kind: str,
        name: str,
        *,
        command: str | None = None,
        project: str | None = None,
        cwd: str | None = None,
        metadata: dict[str, Any] | None = None,
        execution_id: str | None = None,
        trace_id: str | None = None,
    ) -> str:
        """Record the start of an execution and return its id."""
        exec_id = execution_id or new_id()
        self.db.execute(
            "INSERT INTO executions (id, kind, name, command, project, cwd, status, started_at, trace_id, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)",
            (
                exec_id,
                kind,
                name,
                self._r(command),
                project,
                cwd,
                iso_now(),
                trace_id,
                json.dumps({"pid": os.getpid(), **(metadata or {})}, default=str),
            ),
        )
        return exec_id

    def finish(
        self,
        execution_id: str,
        status: str,
        *,
        exit_code: int | None = None,
        duration: float | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record the outcome of an execution."""
        existing = self.db.query_one("SELECT metadata FROM executions WHERE id = ?", (execution_id,))
        merged = json.loads(existing["metadata"]) if existing else {}
        merged.update(metadata or {})
        self.db.execute(
            "UPDATE executions SET status = ?, exit_code = ?, finished_at = ?, duration = ?, error = ?, metadata = ?"
            " WHERE id = ?",
            (
                status,
                exit_code,
                iso_now(),
                duration,
                self._r(error),
                json.dumps(merged, default=str),
                execution_id,
            ),
        )

    def add_step(
        self,
        execution_id: str,
        step_id: str,
        status: str,
        *,
        command: str | None = None,
        exit_code: int | None = None,
        started_at: str | None = None,
        duration: float | None = None,
        error: str | None = None,
        outputs: dict[str, Any] | None = None,
    ) -> None:
        safe_outputs = {k: self._r(str(v)) for k, v in (outputs or {}).items()}
        # One statement, so the sequence number is allocated atomically.
        self.db.execute(
            "INSERT INTO steps (execution_id, seq, step_id, command, status, exit_code, started_at, duration, error, outputs)"
            " SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ? FROM steps WHERE execution_id = ?",
            (
                execution_id,
                step_id,
                self._r(command),
                status,
                exit_code,
                started_at,
                duration,
                self._r(error),
                json.dumps(safe_outputs),
                execution_id,
            ),
        )

    @staticmethod
    def _to_record(row: dict[str, Any]) -> ExecutionRecord:
        data = dict(row)
        data["metadata"] = json.loads(data.get("metadata") or "{}")
        return ExecutionRecord(**data)

    def get(self, execution_id: str) -> ExecutionRecord:
        """Fetch an execution by id or unique id prefix (including its steps)."""
        rows = self.db.query("SELECT * FROM executions WHERE id = ?", (execution_id,))
        if not rows and execution_id:
            prefix = execution_id.replace("%", "").replace("_", "")
            rows = self.db.query(
                "SELECT * FROM executions WHERE id LIKE ? ORDER BY started_at DESC LIMIT 2", (prefix + "%",)
            )
            if len(rows) > 1:
                raise NotFoundError(
                    f"Execution id '{execution_id}' is ambiguous.", hint="Use more characters of the id."
                )
        if not rows:
            raise NotFoundError(
                f"No execution with id '{execution_id}'.", hint="Run `highhx history` to list executions."
            )
        record = self._to_record(rows[0])
        for step in self.db.query("SELECT * FROM steps WHERE execution_id = ? ORDER BY seq", (record.id,)):
            record.steps.append(
                StepRecord(
                    step_id=step["step_id"],
                    status=step["status"],
                    command=step["command"],
                    exit_code=step["exit_code"],
                    started_at=step["started_at"],
                    duration=step["duration"],
                    error=step["error"],
                    outputs=json.loads(step["outputs"] or "{}"),
                )
            )
        return record

    def list(
        self,
        *,
        limit: int = 20,
        kind: str | None = None,
        status: str | None = None,
        name: str | None = None,
    ) -> builtins.list[ExecutionRecord]:
        clauses: builtins.list[str] = []
        params: builtins.list[Any] = []
        for column, value in (("kind", kind), ("status", status), ("name", name)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.query(f"SELECT * FROM executions {where} ORDER BY started_at DESC, rowid DESC LIMIT ?", params)
        return [self._to_record(row) for row in rows]

    def latest(self, *, kind: str | None = None) -> ExecutionRecord | None:
        records = self.list(limit=1, kind=kind)
        return self.get(records[0].id) if records else None

    def stats(self, *, since: str | None = None) -> builtins.list[dict[str, Any]]:
        """Aggregate counts and durations per (kind, name)."""
        where = "WHERE started_at >= ?" if since else ""
        params: tuple[Any, ...] = (since,) if since else ()
        return self.db.query(
            f"""
            SELECT kind, name,
                   COUNT(*) AS runs,
                   SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS succeeded,
                   SUM(CASE WHEN status IN ('failed', 'timeout') THEN 1 ELSE 0 END) AS failed,
                   AVG(duration) AS avg_duration,
                   MAX(started_at) AS last_run
            FROM executions {where}
            GROUP BY kind, name
            ORDER BY runs DESC, name
            """,
            params,
        )

    def mark_stale_running(self) -> int:
        """Mark executions left 'running' by a process that no longer exists as 'cancelled'."""
        from highhx.utils.processes import pid_alive

        count = 0
        for row in self.db.query("SELECT id, metadata FROM executions WHERE status = 'running'"):
            pid = json.loads(row["metadata"] or "{}").get("pid")
            if isinstance(pid, int) and pid_alive(pid):
                continue
            self.db.execute(
                "UPDATE executions SET status = 'cancelled', error = 'process ended unexpectedly' WHERE id = ?",
                (row["id"],),
            )
            count += 1
        return count

    def prune(self, keep: int = 1000) -> int:
        """Delete all but the newest ``keep`` executions."""
        return self.db.execute(
            "DELETE FROM executions WHERE id NOT IN (SELECT id FROM executions ORDER BY started_at DESC LIMIT ?)",
            (keep,),
        )
