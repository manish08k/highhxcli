"""Command benchmarking: run a command repeatedly and report timing statistics."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.core.engine import Engine
from highhx.core.errors import CommandFailedError
from highhx.execution.command import CommandSpec


@dataclass
class BenchmarkResult:
    command: str
    runs: int
    warmup: int
    durations: list[float] = field(default_factory=list)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.durations)

    @property
    def median(self) -> float:
        return statistics.median(self.durations)

    @property
    def stdev(self) -> float:
        return statistics.stdev(self.durations) if len(self.durations) > 1 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "runs": self.runs,
            "warmup": self.warmup,
            "min": round(min(self.durations), 4),
            "max": round(max(self.durations), 4),
            "mean": round(self.mean, 4),
            "median": round(self.median, 4),
            "stdev": round(self.stdev, 4),
            "durations": [round(d, 4) for d in self.durations],
        }


def benchmark(engine: Engine, root: Path, command: str, *, runs: int = 5, warmup: int = 1) -> BenchmarkResult:
    """Run ``command`` ``warmup + runs`` times; every run must succeed."""
    if runs < 1:
        raise ValueError("runs must be >= 1")
    engine.approve(f"Benchmark `{command}` ({runs} runs)", engine_risk(command), policy_action="benchmark")
    result = BenchmarkResult(command, runs, warmup)
    with engine.operation("benchmark", command.split(maxsplit=1)[0] if command.split() else "command", command=command):
        for index in range(warmup + runs):
            outcome = engine.run(
                CommandSpec(command, cwd=root, name="benchmark"), approved=True, echo=False, record=False
            )
            if outcome.dry_run:
                return result
            if not outcome.ok:
                raise CommandFailedError(command, outcome.exit_code or 1, stderr=outcome.stderr)
            if index >= warmup:
                result.durations.append(outcome.duration)
    return result


def engine_risk(command: str):  # type: ignore[no-untyped-def]
    from highhx.approvals.risk import classify_command

    return classify_command(command).risk
