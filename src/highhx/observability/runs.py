"""Automation run traces and metrics — every plain-language request HighhX Free handled.

    run_20260928T101500_3f2a   "open Gmail and search internship"
      decision   deterministic (local) · intent workflow · target gmail · risk safe · executor browser
      step_1     open gmail              ✓ succeeded · verified (Gmail is open)          1.2s
      step_2     search internship       ✓ succeeded · verified (results are showing)    0.9s
      result     succeeded · verified · 2.1s

Runs that were not planned deterministically are traced too (route ``unknown`` or ``pro``: an escalation), so the
metrics can say how often Free handled a request itself. Stored in the history database
(table ``automation_runs``). What is stored is redacted: the request goes through the secret
redactor and typed text is replaced by its length.
"""

from __future__ import annotations

import json
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from highhx.utils.time import iso_now

if TYPE_CHECKING:
    from highhx.decision.deterministic import Decision
    from highhx.plans.runner import PlanOutcome
    from highhx.security.secrets import Redactor
    from highhx.storage.database import Database

_TEXT_PARAMS = ("text",)


def new_run_id() -> str:
    return f"run_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(2)}"


def _redact_params(params: dict[str, Any], redactor: Redactor | None) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in params.items():
        if key in _TEXT_PARAMS:
            clean[key] = f"<{len(str(value))} characters>"
        elif isinstance(value, str) and redactor is not None:
            clean[key] = redactor.redact(value)
        else:
            clean[key] = value
    return clean


def redacted_decision(decision: Decision, redactor: Redactor | None) -> dict[str, Any]:
    data = decision.to_dict()
    if redactor is not None:
        data["request"] = redactor.redact(data["request"])
        data["normalized"] = redactor.redact(data["normalized"])
        data["clauses"] = [redactor.redact(c) for c in data["clauses"]]
        if data.get("unknown"):
            data["unknown"]["clause"] = redactor.redact(data["unknown"]["clause"])
            data["unknown"]["reason"] = redactor.redact(data["unknown"]["reason"])
        data["reason"] = redactor.redact(data["reason"])
        if decision.hxir is not None:
            data["hxir"] = decision.hxir.to_dict(scrub=redactor.redact)
    if data.get("plan"):
        data["plan"]["request"] = data["normalized"]
        for step in data["plan"]["steps"]:
            step["params"] = _redact_params(step["params"], redactor)
    if "text" in data.get("entities", {}):
        data["entities"]["text"] = [f"<{len(data['entities']['text'])} value(s)>"]
    return data


@dataclass
class RunTrace:
    """One run, from decision to outcome. ``finish`` stores it (when there is a database)."""

    decision: Decision
    db: Database | None = None
    redactor: Redactor | None = None
    source: str = "session"
    run_id: str = field(default_factory=new_run_id)
    started_at: str = field(default_factory=iso_now)
    _began: float = field(default_factory=time.monotonic)
    record: dict[str, Any] | None = None

    def finish(self, outcome: PlanOutcome | None = None, *, status: str | None = None) -> dict[str, Any]:
        """Store the run. Without ``outcome``: a request that was not executed (unknown / Pro)."""
        decision = self.decision
        plan = decision.plan
        steps = [
            {**s.to_dict(), "params": _redact_params(dict(s.step.params), self.redactor)}
            for s in (outcome.steps if outcome else [])
        ]
        if outcome is not None:
            final, verification, failure = outcome.status, outcome.verification, outcome.reason
        else:
            final = status or ("escalated" if decision.route == "pro" else decision.route)
            verification, failure = None, decision.reason
        record = {
            "id": self.run_id,
            "started_at": self.started_at,
            "request": self.redactor.redact(decision.request) if self.redactor else decision.request,
            "route": decision.route,
            "intent": decision.intent or None,
            "target": decision.target or None,
            "risk": plan.risk if plan else None,
            "executor": plan.executor if plan else None,
            "status": final,
            "verification": verification,
            "duration": round(time.monotonic() - self._began, 3),
            "failure": failure or None,
            "source": self.source,
            "decision": redacted_decision(decision, self.redactor),
            "steps": steps,
        }
        self.record = record
        if self.db is not None:
            RunStore(self.db).save(record)
        return record


class RunStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, record: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO automation_runs (id, started_at, request, route, intent, target, risk, executor,"
            " status, verification, duration, failure, source, decision, steps)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["id"],
                record["started_at"],
                record["request"],
                record["route"],
                record["intent"],
                record["target"],
                record["risk"],
                record["executor"],
                record["status"],
                record["verification"],
                record["duration"],
                record["failure"],
                record["source"],
                json.dumps(record["decision"], default=str),
                json.dumps(record["steps"], default=str),
            ),
        )

    @staticmethod
    def _row(row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "decision": json.loads(row["decision"] or "{}"), "steps": json.loads(row["steps"] or "[]")}

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM automation_runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))
        return [self._row(r) for r in rows]

    def get(self, run_id: str | None) -> dict[str, Any] | None:
        if not run_id:
            rows = self.list(1)
            return rows[0] if rows else None
        row = self.db.query_one("SELECT * FROM automation_runs WHERE id = ?", (run_id,))
        if row is None:
            matches = self.db.query("SELECT * FROM automation_runs WHERE id LIKE ? LIMIT 2", (f"%{run_id}%",))
            row = matches[0] if len(matches) == 1 else None
        return self._row(row) if row else None

    def metrics(self, limit: int = 1000) -> Metrics:
        return Metrics.of(self.list(limit))


@dataclass
class Metrics:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    unknown: int = 0
    escalations: int = 0
    average_seconds: float | None = None
    action_failures: dict[str, int] = field(default_factory=dict)
    verification_failures: int = 0
    unverified_steps: int = 0
    actions: list[tuple[str, int]] = field(default_factory=list)
    targets: list[tuple[str, int]] = field(default_factory=list)

    @classmethod
    def of(cls, runs: list[dict[str, Any]]) -> Metrics:
        m = cls(total=len(runs))
        durations: list[float] = []
        actions: Counter[str] = Counter()
        targets: Counter[str] = Counter()
        failures: Counter[str] = Counter()
        for run in runs:
            status = run["status"]
            if status == "succeeded":
                m.succeeded += 1
            elif status == "failed":
                m.failed += 1
            elif status == "cancelled":
                m.cancelled += 1
            elif status == "unknown":
                m.unknown += 1
            elif status == "escalated":
                m.escalations += 1
            if run["route"] == "local" and run.get("duration") is not None:
                durations.append(float(run["duration"]))
            if run.get("target"):
                targets[str(run["target"])] += 1
            for step in run["steps"]:
                if step["status"] == "skipped":
                    continue
                actions[step["catalog_action"]] += 1
                verification = step.get("verification") or {}
                if step["status"] not in ("succeeded",):
                    failures[step["catalog_action"]] += 1
                if str(step.get("error", "")).startswith("verification failed"):
                    m.verification_failures += 1
                elif verification.get("status") == "unverified":
                    m.unverified_steps += 1
        m.average_seconds = round(sum(durations) / len(durations), 3) if durations else None
        m.action_failures = dict(failures.most_common())
        m.actions = actions.most_common(10)
        m.targets = targets.most_common(10)
        return m

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_runs": self.total,
            "successful_runs": self.succeeded,
            "failed_runs": self.failed,
            "cancelled_runs": self.cancelled,
            "unknown_requests": self.unknown,
            "pro_escalations": self.escalations,
            "average_seconds": self.average_seconds,
            "action_failures": self.action_failures,
            "verification_failures": self.verification_failures,
            "unverified_steps": self.unverified_steps,
            "most_used_actions": [{"action": a, "runs": n} for a, n in self.actions],
            "most_used_targets": [{"target": t, "runs": n} for t, n in self.targets],
        }
