"""The HighhX benchmark format: tasks, suites, results and the metrics they report.

    suite: browser
    description: …
    tasks:
      - id: export-after-redesign
        description: Export the invoices after the site was redesigned
        environment: {kind: web, variant: redesign, url: "https://shop.test/"}   # initial state
        planner: {kind: scripted, steps: [...]}      # or {kind: model} (optional, real models)
        success: {text: "Export ready"}                # what the agent checks before saying done
        evaluate: [{state: {exported: true}}]          # the benchmark's own, independent check
        ground_truth: {Export: "Download CSV"}         # what each target really is (grounding)
        timeout: 60
        allowed_tools: ["browser.", "computer.state"]
        risk_level: medium                             # approvals above this are declined

Only these metrics are reported: task success rate, grounding accuracy, selector healing rate,
recovery success rate, verification accuracy, average retries, task completion time and
token/tool cost. External datasets (WebArena-, OSWorld- or AndroidWorld-style tasks) can be
converted into this format by an adapter without the core runtime knowing about them.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from highhx.core.errors import ValidationError

ENVIRONMENTS = ("web", "desktop", "android", "workspace", "browser", "android_device")
"""web / desktop / android: deterministic simulations (CI). workspace: a temporary project for
code, file and shell tasks. browser: the real HighhX browser (optional)."""
RISK_LEVELS = ("safe", "low", "medium", "high", "critical")
METRICS = (
    "success",
    "grounding_accuracy",
    "selector_healing_rate",
    "recovery_success_rate",
    "verification_accuracy",
    "average_retries",
    "completion_time",
    "token_cost",
    "tool_cost",
)


@dataclass
class BenchmarkTask:
    id: str
    description: str
    environment: dict[str, Any]
    planner: dict[str, Any]
    success: dict[str, Any] | list[Any] | None = None
    evaluate: list[dict[str, Any]] = field(default_factory=list)
    ground_truth: dict[str, str] = field(default_factory=dict)
    timeout: float = 120.0
    allowed_tools: list[str] = field(default_factory=list)
    risk_level: str = "medium"
    expect_completion: bool = True
    """False for safety tasks: the correct outcome is that the agent does NOT complete."""
    surface: str = ""

    @property
    def kind(self) -> str:
        return str(self.environment.get("kind", "workspace"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkTask:
        problems = validate_task(data)
        if problems:
            raise ValidationError(f"Invalid benchmark task {data.get('id', '?')!r}.", details=problems)
        return cls(
            id=str(data["id"]),
            description=str(data.get("description") or data["id"]),
            environment=dict(data.get("environment") or {"kind": "workspace"}),
            planner=dict(data.get("planner") or {"kind": "scripted", "steps": []}),
            success=data.get("success"),
            evaluate=list(data.get("evaluate") or []),
            ground_truth={str(k): str(v) for k, v in (data.get("ground_truth") or {}).items()},
            timeout=float(data.get("timeout") or 120),
            allowed_tools=[str(t) for t in data.get("allowed_tools") or []],
            risk_level=str(data.get("risk_level") or "medium"),
            expect_completion=bool(data.get("expect_completion", True)),
            surface=str(data.get("surface") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def validate_task(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["a task is a mapping"]
    problems = [f"missing {key}" for key in ("id",) if not data.get(key)]
    env = data.get("environment") or {"kind": "workspace"}
    if not isinstance(env, dict) or env.get("kind", "workspace") not in ENVIRONMENTS:
        problems.append(f"environment.kind must be one of: {', '.join(ENVIRONMENTS)}")
    if isinstance(env, dict) and env.get("kind") == "android_device" and env.get("task") is not None:
        from highhx.benchmarks.environments.android_world import registered

        if env["task"] not in registered():
            problems.append(f"environment.task must be a registered Android task: {', '.join(registered())}")
    planner = data.get("planner") or {}
    if planner and planner.get("kind") not in ("scripted", "model", "resolver"):
        problems.append("planner.kind must be scripted, resolver or model")
    if planner.get("kind") == "scripted" and not isinstance(planner.get("steps"), list):
        problems.append("a scripted planner needs steps")
    if data.get("risk_level", "medium") not in RISK_LEVELS:
        problems.append(f"risk_level must be one of: {', '.join(RISK_LEVELS)}")
    if data.get("success") is not None:
        from highhx.verification.declarative import validate

        problems += validate(data["success"])
    return problems


@dataclass
class BenchmarkSuite:
    name: str
    description: str
    tasks: list[BenchmarkTask]
    path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> BenchmarkSuite:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ValidationError(f"{path.name} is not a benchmark suite (it needs a tasks list).")
        return cls(str(data.get("suite") or path.stem), str(data.get("description") or ""), [BenchmarkTask.from_dict(t) for t in data["tasks"]], path)


@dataclass
class RunMetrics:
    """One run of one task. Rates are None when they do not apply (a task with nothing to
    ground has no grounding accuracy). They are left out of averages, never counted as 0 or 1."""

    benchmark_id: str
    task_id: str
    run: int
    environment: str
    runtime: str
    provider: str
    success: bool
    grounding_accuracy: float | None
    selector_healing_rate: float | None
    recovery_success_rate: float | None
    verification_accuracy: float
    average_retries: float
    completion_time: float
    token_cost: int
    tool_cost: int
    cost_usd: float | None = None
    status: str = ""
    summary: str = ""
    trajectory_id: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)
    """Detail beside the eight metrics (per-action success, latency, attempts, failure categories,
    grounding confidence) — see :func:`highhx.benchmarks.runner.diagnostics`. Not a metric."""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunMetrics:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def aggregate(runs: list[RunMetrics]) -> dict[str, Any]:
    """Suite- or task-level statistics over runs (mean, and spread for time)."""
    if not runs:
        return {"runs": 0}

    def present(name: str) -> list[float]:
        return [float(getattr(r, name)) for r in runs if getattr(r, name) is not None]

    times = [r.completion_time for r in runs]
    return {
        "runs": len(runs),
        "task_success_rate": _mean([1.0 if r.success else 0.0 for r in runs]),
        "grounding_accuracy": _mean(present("grounding_accuracy")),
        "selector_healing_rate": _mean(present("selector_healing_rate")),
        "recovery_success_rate": _mean(present("recovery_success_rate")),
        "verification_accuracy": _mean(present("verification_accuracy")),
        "average_retries": _mean(present("average_retries")),
        "completion_time": {
            "mean": round(statistics.fmean(times), 3),
            "min": round(min(times), 3),
            "max": round(max(times), 3),
            "stdev": round(statistics.stdev(times), 3) if len(times) > 1 else 0.0,
        },
        "token_cost": sum(r.token_cost for r in runs),
        "tool_cost": sum(r.tool_cost for r in runs),
        "cost_usd": round(sum(r.cost_usd for r in runs if r.cost_usd is not None), 6) if any(r.cost_usd is not None for r in runs) else None,
    }


def aggregate_diagnostics(runs: list[RunMetrics]) -> dict[str, Any]:
    """The runs' diagnostics combined (sums for counts, latency over every step of every run)."""
    from highhx.benchmarks.runner import latency

    per_action: dict[str, dict[str, int]] = {}
    failures: dict[str, int] = {}
    attempts: dict[str, int] = {}
    steps: list[float] = []
    actions: list[float] = []
    model: list[float] = []
    grounding: list[float] = []
    verification: list[float] = []
    recoveries = 0
    confidences: list[float] = []
    for run in runs:
        d = run.diagnostics or {}
        for kind, counts in (d.get("per_action") or {}).items():
            entry = per_action.setdefault(kind, {"runs": 0, "succeeded": 0})
            entry["runs"] += int(counts.get("runs") or 0)
            entry["succeeded"] += int(counts.get("succeeded") or 0)
        for name, count in (d.get("failure_categories") or {}).items():
            failures[name] = failures.get(name, 0) + int(count)
        for n, count in (d.get("attempts_per_intent") or {}).items():
            attempts[n] = attempts.get(n, 0) + int(count)
        steps += [float(v) for v in d.get("step_seconds") or []]
        actions += [float(v) for v in d.get("action_seconds") or []]
        if d.get("model_latency_measured"):
            model += [float(v) for v in d.get("planning_seconds") or []]
        grounding += [float(v) for v in d.get("grounding_seconds") or []]
        verification += [float(v) for v in d.get("verification_seconds") or []]
        recoveries += int(d.get("recoveries") or 0)
        if d.get("grounding_confidence") is not None:
            confidences.append(float(d["grounding_confidence"]))
    return {
        "per_action": {
            k: {**v, "success_rate": round(v["succeeded"] / v["runs"], 4) if v["runs"] else None}
            for k, v in sorted(per_action.items())
        },
        "step_latency": latency(steps),
        "action_latency": latency(actions),
        "model_latency": latency(model),
        "grounding_latency": latency(grounding),
        "verification_latency": latency(verification),
        "recoveries": recoveries,
        "attempts_per_intent": dict(sorted(attempts.items(), key=lambda kv: int(kv[0]))),
        "failure_categories": dict(sorted(failures.items())),
        "grounding_confidence": _mean(confidences),
    }


@dataclass
class BenchmarkResult:
    benchmark_id: str
    suite: str
    started: float
    runs: list[RunMetrics]
    provider: str = "scripted"
    runtime: str = "local"
    environment: dict[str, Any] = field(default_factory=dict)
    """Platform and capability availability where it ran (comparable across machines)."""
    skipped: list[dict[str, str]] = field(default_factory=list)
    """Tasks that could not run here (``{task, reason}``): neither passed nor failed."""

    def by_task(self) -> dict[str, dict[str, Any]]:
        tasks: dict[str, list[RunMetrics]] = {}
        for run in self.runs:
            tasks.setdefault(run.task_id, []).append(run)
        return {task: aggregate(runs) for task, runs in tasks.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "suite": self.suite,
            "started": self.started,
            "provider": self.provider,
            "runtime": self.runtime,
            "environment": self.environment,
            "skipped": self.skipped,
            "summary": aggregate(self.runs),
            "diagnostics": aggregate_diagnostics(self.runs),  # beside the metrics, never among them
            "tasks": self.by_task(),
            "runs": [r.to_dict() for r in self.runs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkResult:
        return cls(
            str(data["benchmark_id"]),
            str(data.get("suite", "")),
            float(data.get("started") or 0),
            [RunMetrics.from_dict(r) for r in data.get("runs") or []],
            str(data.get("provider") or "scripted"),
            str(data.get("runtime") or "local"),
            dict(data.get("environment") or {}),
            list(data.get("skipped") or []),
        )
