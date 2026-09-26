"""Deployment state tracking."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from highhx.core.errors import NotFoundError
from highhx.storage.database import Database
from highhx.utils.hashing import new_id
from highhx.utils.time import iso_now


class DeployStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


@dataclass
class DeploymentRecord:
    id: str
    target: str
    strategy: str
    status: str
    started_at: str
    version: str | None = None
    git_sha: str | None = None
    finished_at: str | None = None
    execution_id: str | None = None
    rollback_of: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class DeploymentStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _record(row: dict[str, Any]) -> DeploymentRecord:
        data = dict(row)
        data["details"] = json.loads(data.get("details") or "{}")
        return DeploymentRecord(**data)

    def create(
        self,
        target: str,
        strategy: str,
        *,
        version: str | None,
        git_sha: str | None,
        execution_id: str | None,
        rollback_of: str | None = None,
    ) -> DeploymentRecord:
        record = DeploymentRecord(
            id=new_id("dep-"),
            target=target,
            strategy=strategy,
            status=DeployStatus.RUNNING,
            started_at=iso_now(),
            version=version,
            git_sha=git_sha,
            execution_id=execution_id,
            rollback_of=rollback_of,
        )
        self.db.execute(
            "INSERT INTO deployments (id, target, version, git_sha, strategy, status, started_at, execution_id, rollback_of, details)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')",
            (
                record.id,
                target,
                version,
                git_sha,
                strategy,
                str(record.status),
                record.started_at,
                execution_id,
                rollback_of,
            ),
        )
        return record

    def finish(self, deployment_id: str, status: DeployStatus, details: dict[str, Any] | None = None) -> None:
        existing = self.get(deployment_id)
        merged = {**existing.details, **(details or {})}
        self.db.execute(
            "UPDATE deployments SET status = ?, finished_at = ?, details = ? WHERE id = ?",
            (str(status), iso_now(), json.dumps(merged, default=str), deployment_id),
        )

    def mark(self, deployment_id: str, status: DeployStatus) -> None:
        self.db.execute("UPDATE deployments SET status = ? WHERE id = ?", (str(status), deployment_id))

    def get(self, deployment_id: str) -> DeploymentRecord:
        row = self.db.query_one("SELECT * FROM deployments WHERE id = ?", (deployment_id,))
        if row is None:
            rows = self.db.query("SELECT * FROM deployments WHERE id LIKE ? LIMIT 2", (deployment_id + "%",))
            if len(rows) != 1:
                raise NotFoundError(
                    f"No deployment with id '{deployment_id}'.", hint="Run `highhx deploy status` to list deployments."
                )
            row = rows[0]
        return self._record(row)

    def history(self, target: str | None = None, *, limit: int = 20) -> list[DeploymentRecord]:
        if target:
            rows = self.db.query(
                "SELECT * FROM deployments WHERE target = ? ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (target, limit),
            )
        else:
            rows = self.db.query("SELECT * FROM deployments ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))
        return [self._record(r) for r in rows]

    def latest(self, target: str) -> DeploymentRecord | None:
        records = self.history(target, limit=1)
        return records[0] if records else None

    def successful(self, target: str) -> list[DeploymentRecord]:
        rows = self.db.query(
            "SELECT * FROM deployments WHERE target = ? AND status = 'succeeded' ORDER BY started_at DESC, rowid DESC",
            (target,),
        )
        return [self._record(r) for r in rows]
