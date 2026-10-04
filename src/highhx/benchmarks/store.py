"""Benchmark results on disk, reports and comparisons."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from highhx.benchmarks.model import BenchmarkResult
from highhx.core.errors import NotFoundError

COMPARED = (
    "task_success_rate",
    "grounding_accuracy",
    "selector_healing_rate",
    "recovery_success_rate",
    "verification_accuracy",
    "average_retries",
)
HIGHER_IS_BETTER = {
    "task_success_rate": True,
    "grounding_accuracy": True,
    "selector_healing_rate": True,
    "recovery_success_rate": True,
    "verification_accuracy": True,
    "average_retries": False,
    "completion_time": False,
    "token_cost": False,
    "tool_cost": False,
}


class BenchmarkStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def for_app(cls, app: Any) -> BenchmarkStore:
        from highhx.utils.paths import user_data_dir

        root = (app.paths.state_dir / "benchmarks") if getattr(app, "initialized", False) else user_data_dir() / "benchmarks"
        return cls(root)

    def save(self, result: BenchmarkResult) -> Path:
        from highhx.utils.filesystem import atomic_write_text

        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{result.benchmark_id}.json"
        atomic_write_text(path, json.dumps(result.to_dict(), indent=1))
        return path

    def load(self, benchmark_id: str | None = None) -> BenchmarkResult:
        if benchmark_id in (None, "", "latest"):
            files = sorted(self.root.glob("bench_*.json"), key=lambda p: p.stat().st_mtime) if self.root.is_dir() else []
            if not files:
                raise NotFoundError("No benchmark results yet.", hint="Run `highhx benchmark run <suite>`.")
            path = files[-1]
        else:
            path = self.root / f"{benchmark_id}.json"
            if not path.is_file():
                raise NotFoundError(f"No benchmark result {benchmark_id!r}.", hint="See `highhx benchmark report --list`.")
        return BenchmarkResult.from_dict(json.loads(path.read_text()))

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return [p.stem for p in sorted(self.root.glob("bench_*.json"), key=lambda p: p.stat().st_mtime)]


def compare(base: BenchmarkResult, other: BenchmarkResult) -> dict[str, Any]:
    """Metric deltas (other - base) for the suite and for each task both ran."""
    a, b = base.to_dict(), other.to_dict()

    def delta(x: dict[str, Any], y: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for metric in (*COMPARED, "completion_time", "token_cost", "tool_cost"):
            left, right = x.get(metric), y.get(metric)
            if isinstance(left, dict):
                left, right = left.get("mean"), (right or {}).get("mean")
            if left is None or right is None:
                out[metric] = {"base": left, "other": right, "delta": None, "better": None}
                continue
            change = round(float(right) - float(left), 4)
            better = None if change == 0 else (change > 0) == HIGHER_IS_BETTER[metric]
            out[metric] = {"base": left, "other": right, "delta": change, "better": better}
        return out

    tasks = {t: delta(a["tasks"][t], b["tasks"][t]) for t in a["tasks"] if t in b["tasks"]}
    return {"base": base.benchmark_id, "other": other.benchmark_id, "summary": delta(a["summary"], b["summary"]), "tasks": tasks}
