"""Audit trail of every action HighhX automation decided on and performed.

Shared by deterministic automation (Free) and the agent (Pro). Rows are
append-only, redacted before they are written, and never contain secret values.
"""

from __future__ import annotations

import builtins
import json
import time
from dataclasses import dataclass, field
from typing import Any

from highhx.safety.actions import ActionDescriptor
from highhx.safety.classifier import SafetyVerdict
from highhx.security.secrets import Redactor
from highhx.storage.database import Database
from highhx.utils.hashing import new_id
from highhx.utils.time import iso_now


@dataclass
class AuditEvent:
    source: str
    """agent | computer | do"""
    action: ActionDescriptor
    decision: str
    """allowed | confirmed | denied | blocked | policy | invalid"""
    status: str = "pending"
    """ok | failed | error | cancelled | timeout | skipped"""
    verdict: SafetyVerdict | None = None
    session_id: str | None = None
    account_id: str | None = None
    ticket_id: str | None = None
    verified: bool | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)


@dataclass
class AuditRecord:
    id: str
    created_at: str
    source: str
    session_id: str | None
    account_id: str | None
    actor: str
    tool: str
    kind: str
    action: str
    target: str
    risk: str
    categories: list[str]
    decision: str
    ticket_id: str | None
    status: str
    verified: bool | None
    duration: float | None
    error: str | None
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class AuditLog:
    def __init__(self, db: Database, redactor: Redactor | None = None) -> None:
        self.db = db
        self.redactor = redactor or Redactor()

    def _r(self, value: str | None) -> str | None:
        return self.redactor.redact(value) if value else value

    def record(self, event: AuditEvent) -> str:
        verdict = event.verdict
        action = event.action
        record_id = new_id("a-")
        details = json.loads(self.redactor.redact(json.dumps(event.details, default=str)))
        if action.command:
            details.setdefault("command", self._r(action.command))
        self.db.execute(
            "INSERT INTO audit_log (id, created_at, source, session_id, account_id, actor, tool, kind, action, target,"
            " risk, categories, decision, ticket_id, status, verified, duration, error, details)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                iso_now(),
                event.source,
                event.session_id,
                event.account_id,
                str(action.actor),
                action.tool,
                str(action.kind),
                self._r(action.summary),
                self._r(action.target),
                verdict.risk.label if verdict else None,
                json.dumps(sorted(verdict.categories) if verdict else []),
                event.decision,
                event.ticket_id,
                event.status,
                None if event.verified is None else int(event.verified),
                round(time.monotonic() - event.started, 3),
                self._r(event.error),
                json.dumps(details),
            ),
        )
        return record_id

    def list(
        self, *, limit: int = 50, session_id: str | None = None, account_id: str | None = None
    ) -> builtins.list[AuditRecord]:
        clauses, params = [], []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if account_id:
            clauses.append("(account_id = ? OR account_id IS NULL)")
            params.append(account_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query(
            f"SELECT * FROM audit_log {where} ORDER BY created_at DESC, rowid DESC LIMIT ?",  # nosec B608 - constant clauses
            (*params, limit),
        )
        return [
            AuditRecord(
                id=r["id"],
                created_at=r["created_at"],
                source=r["source"],
                session_id=r["session_id"],
                account_id=r["account_id"],
                actor=r["actor"],
                tool=r["tool"],
                kind=r["kind"],
                action=r["action"],
                target=r["target"] or "",
                risk=r["risk"] or "",
                categories=json.loads(r["categories"] or "[]"),
                decision=r["decision"],
                ticket_id=r["ticket_id"],
                status=r["status"],
                verified=None if r["verified"] is None else bool(r["verified"]),
                duration=r["duration"],
                error=r["error"],
                details=json.loads(r["details"] or "{}"),
            )
            for r in rows
        ]
