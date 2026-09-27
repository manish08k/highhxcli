"""Agent sessions and transcripts, stored in the HighhX state database.

Transcripts stay on the developer's machine (redacted); only session metadata
(title, provider, model, usage, status) is synced to the HighhX platform.
"""

from __future__ import annotations

import builtins
import json
import os
import socket
from dataclasses import dataclass, field
from typing import Any

from highhx.agent.messages import Message, Usage
from highhx.agent.planner import Plan
from highhx.core.errors import HighhXError, NotFoundError
from highhx.security.secrets import Redactor
from highhx.storage.database import Database
from highhx.utils.hashing import new_id
from highhx.utils.time import iso_now


@dataclass
class SessionRecord:
    id: str
    title: str
    root: str
    provider: str
    model: str | None
    status: str
    created_at: str
    updated_at: str
    turns: int = 0
    usage: Usage = field(default_factory=Usage)
    plan: Plan | None = None
    remote_id: str | None = None
    account_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "root": self.root,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turns": self.turns,
            "usage": self.usage.to_dict(),
            "plan": self.plan.to_dict() if self.plan else None,
        }


def _record(row: dict[str, Any]) -> SessionRecord:
    plan = json.loads(row["plan"]) if row.get("plan") else None
    return SessionRecord(
        id=row["id"],
        title=row["title"],
        root=row["root"],
        provider=row["provider"],
        model=row["model"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        turns=int(row["turns"] or 0),
        usage=Usage.from_dict(json.loads(row["usage"] or "{}")),
        plan=Plan.from_dict(plan) if isinstance(plan, dict) else None,
        remote_id=row.get("remote_id"),
        account_id=row.get("account_id"),
    )


class SessionStore:
    def __init__(self, db: Database, redactor: Redactor | None = None) -> None:
        self.db = db
        self.redactor = redactor or Redactor()

    def create(
        self, *, title: str, root: str, provider: str, model: str | None, account_id: str | None = None
    ) -> SessionRecord:
        now = iso_now()
        record = SessionRecord(
            new_id("s-"), title[:200], root, provider, model, "created", now, now, account_id=account_id
        )
        self.db.execute(
            "INSERT INTO agent_sessions (id, title, root, provider, model, status, created_at, updated_at, account_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.id, record.title, root, provider, model, "created", now, now, account_id),
        )
        return record

    def update(self, record: SessionRecord) -> None:
        record.updated_at = iso_now()
        self.db.execute(
            "UPDATE agent_sessions SET title = ?, provider = ?, model = ?, status = ?, updated_at = ?, turns = ?,"
            " usage = ?, plan = ?, remote_id = ? WHERE id = ?",
            (
                record.title[:200],
                record.provider,
                record.model,
                record.status,
                record.updated_at,
                record.turns,
                json.dumps(record.usage.to_dict()),
                json.dumps(record.plan.to_dict()) if record.plan else None,
                record.remote_id,
                record.id,
            ),
        )

    def append(self, session_id: str, message: Message) -> None:
        # Tool output and model text can echo secrets from command output; redact before storing.
        content = self.redactor.redact(json.dumps(message.to_dict(), default=str))
        with self.db.transaction():
            row = self.db.query_one(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM agent_messages WHERE session_id = ?", (session_id,)
            )
            seq = int(row["seq"] if row else 0) + 1
            self.db.execute(
                "INSERT INTO agent_messages (session_id, seq, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, seq, message.role, content, iso_now()),
            )

    def get(self, session_id: str, *, account_id: str | None = None) -> SessionRecord:
        """A session by id (or unique id fragment). With ``account_id``, only that account's sessions
        are visible — another HighhX account on the same machine cannot resume them."""
        owner = "AND (account_id = ? OR account_id IS NULL)" if account_id else ""
        extra: tuple[str, ...] = (account_id,) if account_id else ()
        row = self.db.query_one(f"SELECT * FROM agent_sessions WHERE id = ? {owner}", (session_id, *extra))  # nosec B608 - only constant SQL clauses are interpolated; values are bound parameters
        if row is None:
            matches = self.db.query(
                f"SELECT * FROM agent_sessions WHERE id LIKE ? {owner} LIMIT 2",  # nosec B608 - only constant SQL clauses are interpolated; values are bound parameters
                (f"%{session_id}%", *extra),  # nosec B608 - only constant SQL clauses are interpolated; values are bound parameters
            )
            if len(matches) != 1:
                raise NotFoundError(
                    f"No agent session '{session_id}'.", hint="List sessions with `highhx agent sessions`."
                )
            row = matches[0]
        return _record(row)

    def latest(self, root: str, *, account_id: str | None = None) -> SessionRecord | None:
        owner = "AND (account_id = ? OR account_id IS NULL)" if account_id else ""
        extra: tuple[str, ...] = (account_id,) if account_id else ()
        row = self.db.query_one(
            f"SELECT * FROM agent_sessions WHERE root = ? AND turns > 0 {owner} ORDER BY updated_at DESC, id DESC LIMIT 1",  # nosec B608 - only constant SQL clauses are interpolated; values are bound parameters
            (root, *extra),
        )
        return _record(row) if row else None

    def list(
        self, *, root: str | None = None, limit: int = 20, account_id: str | None = None
    ) -> builtins.list[SessionRecord]:
        clauses: builtins.list[str] = []
        params: builtins.list[object] = []
        if root is not None:
            clauses.append("root = ?")
            params.append(root)
        if account_id:
            clauses.append("(account_id = ? OR account_id IS NULL)")
            params.append(account_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query(
            f"SELECT * FROM agent_sessions {where} ORDER BY updated_at DESC, id DESC LIMIT ?",  # nosec B608 - only constant SQL clauses are interpolated; values are bound parameters
            (*params, limit),  # nosec B608 - only constant SQL clauses are interpolated; values are bound parameters
        )
        return [_record(r) for r in rows]

    def messages(self, session_id: str) -> builtins.list[Message]:
        rows = self.db.query("SELECT content FROM agent_messages WHERE session_id = ? ORDER BY seq", (session_id,))
        out: builtins.list[Message] = []
        for row in rows:
            try:
                out.append(Message.from_dict(json.loads(row["content"])))
            except (ValueError, KeyError):
                continue
        return out

    # ------------------------------------------------------------------ lease
    def acquire(self, session_id: str, owner: str) -> None:
        """Take the session for this process. Raises :class:`SessionBusyError` if another live
        HighhX process holds it; a holder that died (same host, pid gone) is replaced."""
        taken = self.db.execute(
            "UPDATE agent_sessions SET lease_owner = ? WHERE id = ? AND (lease_owner IS NULL OR lease_owner = ?)",
            (owner, session_id, owner),
        )
        if taken:
            return
        row = self.db.query_one("SELECT lease_owner FROM agent_sessions WHERE id = ?", (session_id,))
        holder = str(row["lease_owner"]) if row and row["lease_owner"] else ""
        if holder and _stale(holder):
            if self.db.execute(
                "UPDATE agent_sessions SET lease_owner = ? WHERE id = ? AND lease_owner = ?",
                (owner, session_id, holder),
            ):
                return
        raise SessionBusyError(
            f"Agent session {session_id} is in use by another HighhX process ({holder}).",
            hint="Stop that process (`highhx agent stop`) or start a new session.",
        )

    def release(self, session_id: str, owner: str) -> None:
        self.db.execute(
            "UPDATE agent_sessions SET lease_owner = NULL WHERE id = ? AND lease_owner = ?", (session_id, owner)
        )

    def delete(self, session_id: str) -> bool:
        return self.db.execute("DELETE FROM agent_sessions WHERE id = ?", (session_id,)) > 0


class SessionBusyError(HighhXError):
    """Another process is driving this agent session."""

    category = "session_busy"


def lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _stale(holder: str) -> bool:
    host, _, pid = holder.rpartition(":")
    if host != socket.gethostname() or not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False
