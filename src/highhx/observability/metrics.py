"""Metrics derived from execution history."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from highhx.storage.database import Database
from highhx.utils.time import utc_now


@dataclass
class NameMetrics:
    """Aggregates for one command/workflow name."""

    kind: str
    name: str
    runs: int
    succeeded: int
    failed: int
    durations: list[float] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.runs if self.runs else 0.0

    def percentile(self, pct: float) -> float | None:
        if not self.durations:
            return None
        ordered = sorted(self.durations)
        index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
        return ordered[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "runs": self.runs,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "success_rate": round(self.success_rate, 4),
            "median_duration": round(statistics.median(self.durations), 3) if self.durations else None,
            "p95_duration": round(self.percentile(95) or 0, 3) if self.durations else None,
        }


@dataclass
class MetricsReport:
    """Summary over a time window."""

    since: str
    total: int
    succeeded: int
    failed: int
    by_name: list[NameMetrics]

    def to_dict(self) -> dict[str, Any]:
        return {
            "since": self.since,
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "success_rate": round(self.succeeded / self.total, 4) if self.total else None,
            "by_name": [m.to_dict() for m in self.by_name],
        }


def compute_metrics(db: Database, *, days: int = 30) -> MetricsReport:
    """Aggregate executions from the last ``days`` days."""
    since = (utc_now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.query(
        "SELECT kind, name, status, duration FROM executions WHERE started_at >= ? AND status != 'running'",
        (since,),
    )
    groups: dict[tuple[str, str], NameMetrics] = {}
    for row in rows:
        key = (row["kind"], row["name"])
        metric = groups.setdefault(key, NameMetrics(row["kind"], row["name"], 0, 0, 0))
        metric.runs += 1
        if row["status"] == "success":
            metric.succeeded += 1
        elif row["status"] in ("failed", "timeout"):
            metric.failed += 1
        if row["duration"] is not None:
            metric.durations.append(float(row["duration"]))
    by_name = sorted(groups.values(), key=lambda m: (-m.runs, m.name))
    return MetricsReport(
        since=since,
        total=sum(m.runs for m in by_name),
        succeeded=sum(m.succeeded for m in by_name),
        failed=sum(m.failed for m in by_name),
        by_name=by_name,
    )
