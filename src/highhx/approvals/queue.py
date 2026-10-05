"""Approvals answered from anywhere (the web console, the API): a queue behind the gate's prompter.

The gate and the confirmation broker ask a prompter; :class:`QueuePrompter` is one that puts the
question in a :class:`ApprovalQueue` and waits for a decision:

    approve      the action runs (a critical one needs the typed word, checked here)
    reject       it does not run (audited as denied)
    modify       it does not run as asked; the new inputs go back to the executor, which validates,
                 classifies and asks for them again — a modification is never approved implicitly
    defer        ask again later: the deadline moves (at most MAX_DEFERS times)
    timeout      no answer before the deadline: rejected

Nothing here decides risk or policy: the gate already did, and its audit row records the outcome.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields
from typing import Any

from highhx.core.errors import ApprovalDeniedError

DEFAULT_TIMEOUT = 300.0
MAX_DEFERS = 3
DECISIONS = ("approve", "reject", "modify", "defer")


class ApprovalModifiedError(ApprovalDeniedError):
    """The person changed the action's inputs instead of approving it."""

    def __init__(self, message: str, inputs: dict[str, Any]) -> None:
        super().__init__(message)
        self.inputs = inputs


@dataclass
class PendingApproval:
    id: str
    kind: str
    """``permission`` (a normal-risk change) or ``confirm`` (a sensitive action)."""
    action: str
    target: str
    tool: str
    risk: str
    reasons: list[str]
    details: list[str]
    confirm_word: str | None
    created: float
    deadline: float
    task_id: str = ""
    trace_id: str = ""
    status: str = "pending"
    """pending · approved · rejected · modified · expired"""
    decided_at: float | None = None
    decided_by: str = ""
    defers: int = 0
    note: str = ""
    inputs: dict[str, Any] | None = None
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self) if not f.name.startswith("_")}


class ApprovalQueue:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        emit: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.timeout = timeout
        self.emit = emit or (lambda *_a, **_k: None)
        self.clock = clock
        self._items: dict[str, PendingApproval] = {}
        self._lock = threading.Lock()

    def pending(self) -> list[PendingApproval]:
        with self._lock:
            return [p for p in self._items.values() if p.status == "pending"]

    def all(self, limit: int = 100) -> list[PendingApproval]:
        with self._lock:
            return sorted(self._items.values(), key=lambda p: p.created, reverse=True)[:limit]

    def get(self, approval_id: str) -> PendingApproval:
        with self._lock:
            found = self._items.get(approval_id)
        if found is None:
            raise KeyError(approval_id)
        return found

    def ask(self, item: PendingApproval) -> PendingApproval:
        """Queue ``item`` and wait until it is decided or its deadline passes (rejected)."""
        with self._lock:
            self._items[item.id] = item
        self.emit("approval.required", approval=item.id, action=item.action, risk=item.risk, kind=item.kind)
        while not item._done.wait(0.1):
            if self.clock() > item.deadline:
                with self._lock:
                    if item.status == "pending":
                        item.status, item.decided_at, item.note = "expired", self.clock(), "no answer in time"
                        item._done.set()
                self.emit("approval.expired", approval=item.id, action=item.action)
        return item

    def decide(
        self,
        approval_id: str,
        decision: str,
        *,
        by: str = "",
        typed: str = "",
        inputs: dict[str, Any] | None = None,
        note: str = "",
    ) -> PendingApproval:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
        item = self.get(approval_id)
        with self._lock:
            if item.status != "pending":
                raise ValueError(f"already {item.status}")
            if decision == "defer":
                if item.defers >= MAX_DEFERS:
                    raise ValueError(f"deferred {MAX_DEFERS} times already; approve, reject or modify it")
                item.defers += 1
                item.deadline = self.clock() + self.timeout
                self.emit("approval.deferred", approval=item.id, until=item.deadline)
                return item
            if decision == "approve" and item.confirm_word and typed.strip().lower() != item.confirm_word:
                raise ValueError(f"type {item.confirm_word!r} to approve a critical action")
            if decision == "modify":
                if item.kind != "confirm" or not inputs:
                    raise ValueError("modify needs the new inputs of an action")
                item.inputs = dict(inputs)
            item.status = {"approve": "approved", "reject": "rejected", "modify": "modified"}[decision]
            item.decided_at, item.decided_by, item.note = self.clock(), by, note
            item._done.set()
        self.emit(f"approval.{item.status}", approval=item.id, action=item.action, by=by)
        return item


class QueuePrompter:
    """The gate's prompter (``ask_permission`` and ``confirm_action``) backed by a queue."""

    interactive = True

    def __init__(self, queue: ApprovalQueue) -> None:
        self.queue = queue

    def _item(
        self,
        kind: str,
        action: str,
        target: str,
        tool: str,
        risk: str,
        reasons: Sequence[str],
        details: Sequence[str],
        word: str | None,
    ) -> PendingApproval:
        from highhx.core.events import current_context

        context = current_context()
        now = self.queue.clock()
        return PendingApproval(
            "apr_" + secrets.token_hex(6),
            kind,
            action,
            target,
            tool,
            risk,
            list(reasons),
            list(details),
            word,
            now,
            now + self.queue.timeout,
            task_id=str(context.get("task_id") or ""),
            trace_id=str(context.get("trace_id") or ""),
        )

    def ask_permission(self, action: str, details: Sequence[str], *, allow_always: bool = True) -> str:
        item = self.queue.ask(self._item("permission", action, "", "", "normal", (), details, None))
        return "yes" if item.status == "approved" else "no"

    def confirm_action(self, request: Any) -> bool:
        item = self.queue.ask(
            self._item(
                "confirm",
                request.action,
                request.target,
                request.tool,
                request.risk_name,
                request.reasons,
                request.details,
                request.confirm_word,
            )
        )
        if item.status == "modified":
            raise ApprovalModifiedError(f"Modified: {request.action}", item.inputs or {})
        return item.status == "approved"

    def confirm(self, message: str, *, default: bool = False) -> bool:
        item = self.queue.ask(self._item("permission", message, "", "", "normal", (), (), None))
        return item.status == "approved"

    def confirm_typed(self, message: str, expected: str) -> bool:
        item = self.queue.ask(self._item("confirm", message, "", "", "critical", (), (), expected))
        return item.status == "approved"
