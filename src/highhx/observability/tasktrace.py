"""Task traces: everything a task did, reconstructable afterwards, built only from its events.

    Task  "export the invoices"                         tr_9c1… · task_51a… · completed
    ├── Plan                                            open the invoices · export the invoices
    ├── Step 1  open the invoices
    │   ├── Observation   browser · 6 elements
    │   ├── Action        browser.open  risk low · not asked · ok 0.2s   (execution 2026…-a1b2c3d4e5f6)
    │   └── Verification  success
    ├── Step 2  export the invoices
    │   ├── Grounding     accessibility failed → dom success
    │   ├── Healed        "Export" → "Download CSV" (dom, 0.95)
    │   ├── Action        browser.click  risk low · ok
    │   ├── Verification  success
    │   └── Reflection    continue: verified
    └── Result  completed — all 2 step(s) done

The trace answers: what was asked, what was planned, what was observed, which actions ran (with
risk, approval and the history entry), what failed, what was retried, recovered or healed, what
was verified and how the task finished. Traces are stored as redacted JSON Lines (one per trace
id) next to the trajectories, and ``highhx trace show <id>`` renders them.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from highhx.core.errors import NotFoundError, ValidationError
from highhx.observability.stream import EventRecord, EventRecorder

if TYPE_CHECKING:
    from highhx.core.events import EventBus
    from highhx.security.secrets import Redactor


OBSERVATION_ACTIONS = frozenset({"computer.state", "android.observe"})


@dataclass
class TraceNode:
    kind: str
    label: str
    detail: str = ""
    status: str = ""
    children: list[TraceNode] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def add(self, node: TraceNode) -> TraceNode:
        self.children.append(node)
        return node

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "detail": self.detail,
            "status": self.status,
            "children": [c.to_dict() for c in self.children],
        }

    def render(self, prefix: str = "", last: bool = True, root: bool = True) -> list[str]:
        head = f"{self.kind.capitalize():<13} {self.label}".rstrip()
        if self.status:
            head += f"  [{self.status}]"
        if self.detail:
            head += f"  — {self.detail}"
        lines = [head] if root else [f"{prefix}{'└── ' if last else '├── '}{head}"]
        child_prefix = "" if root else prefix + ("    " if last else "│   ")
        for i, child in enumerate(self.children):
            lines += child.render(child_prefix, i == len(self.children) - 1, False)
        return lines


@dataclass
class TaskTrace:
    trace_id: str
    records: list[EventRecord]

    @property
    def task_id(self) -> str:
        return next((r.task_id for r in self.records if r.task_id), "")

    @property
    def goal(self) -> str:
        start = self._first("task.started") or self._first("agent.started")
        return str(start.payload.get("task", "")) if start else ""

    @property
    def status(self) -> str:
        end = self._last("task.completed") or self._last("task.failed")
        return str(end.payload.get("status", "")) if end else "running"

    def _first(self, name: str) -> EventRecord | None:
        return next((r for r in self.records if r.name == name), None)

    def _last(self, name: str) -> EventRecord | None:
        return next((r for r in reversed(self.records) if r.name == name), None)

    def tree(self) -> TraceNode:
        root = TraceNode("task", repr(self.goal) if self.goal else self.trace_id, f"{self.trace_id} · {self.task_id}", self.status)
        plans = [r for r in self.records if r.name == "plan.created"]
        if plans:
            planned = plans[-1].payload.get("steps") or []
            root.add(TraceNode("plan", f"{len(planned)} step(s)", " · ".join(str(s) for s in planned)[:300]))
        steps: dict[str, TraceNode] = {}
        actions: dict[str, TraceNode] = {}
        for record in self.records:
            step = None
            if record.step_id:
                step = steps.get(record.step_id)
                if step is None:
                    step = steps[record.step_id] = root.add(TraceNode("step", f"{len(steps) + 1}", ""))
            parent = step or root
            name, p = record.name, record.payload
            if name == "plan.updated" and step is not None:
                step.label = f"{list(steps).index(record.step_id) + 1}  {p.get('current', '')}"
                if p.get("status") in ("done", "failed"):
                    step.status = str(p["status"])
            elif name == "observation.created" and step is not None:
                parent.add(TraceNode("observation", f"{p.get('surface', '')} · {p.get('elements', 0)} element(s)", str(p.get("url") or p.get("app") or "")))
            elif name == "grounding.completed":
                attempts = " → ".join(f"{a.get('strategy')} {a.get('result')}" for a in p.get("attempts") or [])
                parent.add(TraceNode("grounding", repr(p.get("target", "")), attempts, str(p.get("status", ""))))
            elif name == "selector.healed":
                parent.add(TraceNode("healed", f"{p.get('was')!r} → {p.get('now')!r}", f"{p.get('strategy')}, {p.get('reason', '')}"))
            elif name in ("action.planned", "action.completed", "action.failed") and p.get("action") in OBSERVATION_ACTIONS:
                continue  # shown as the observation it produced
            elif name == "action.planned":
                node = parent.add(TraceNode("action", str(p.get("action", "")), f"risk {p.get('risk')} · {'asked' if p.get('asks') else 'not asked'}"))
                actions[record.action_id or f"open:{id(parent)}:{p.get('action')}"] = node
            elif name in ("approval.granted", "approval.denied"):
                target = actions.get(record.action_id)
                if target is not None:
                    target.detail += f" · {name.split('.')[1]}"
            elif name in ("action.completed", "action.failed"):
                target = actions.pop(record.action_id or f"open:{id(parent)}:{p.get('action')}", None) or parent.add(TraceNode("action", str(p.get("action", ""))))
                target.status = "ok" if name == "action.completed" else str(p.get("status") or "failed")
                if p.get("seconds") is not None:
                    target.detail += f" · {float(p['seconds']):.2f}s"
                if p.get("error"):
                    target.detail += f" · {p['error']}"
                if p.get("execution_id"):
                    target.data["execution_id"] = p["execution_id"]
                    target.detail += f" (execution {p['execution_id']})"
            elif name == "action.retry":
                parent.add(TraceNode("retry", str(p.get("action", "")), f"attempt {p.get('attempt')} · {p.get('error', '')}"))
            elif name == "verification.completed":
                parent.add(TraceNode("verification", str(p.get("outcome") or p.get("verdict") or ""), f"{p.get('observations', 1)} observation(s)" if p.get("observations") else ""))
            elif name in ("recovery.started",):
                parent.add(TraceNode("recovery", str(p.get("kind", "")), str(p.get("reason", ""))))
            elif name == "agent.reflection":
                if step is not None and not step.detail:
                    step.detail = str(p.get("lesson") or "")
                parent.add(TraceNode("reflection", str(p.get("decision", "")), str(p.get("reason", ""))))
            elif name == "checkpoint.resumed":
                root.add(TraceNode("resumed", f"after {p.get('steps', 0)} step(s)"))
            elif name == "model.usage":
                parent.add(TraceNode("model", str(p.get("model", "")), f"{p.get('input_tokens', 0)} in / {p.get('output_tokens', 0)} out"))
            elif name in ("task.completed", "task.failed"):
                root.add(TraceNode("result", str(p.get("status", "")), str(p.get("summary", ""))))
        return root

    def render(self) -> str:
        return "\n".join(self.tree().render())

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status,
            "tree": self.tree().to_dict(),
            "events": [r.to_dict() for r in self.records],
        }


class TraceStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def for_app(cls, app: Any) -> TraceStore:
        from highhx.utils.paths import user_data_dir

        root = (app.paths.state_dir / "traces") if getattr(app, "initialized", False) else user_data_dir() / "traces"
        return cls(root)

    def _path(self, trace_id: str) -> Path:
        if not trace_id.replace("_", "").isalnum():
            raise ValidationError(f"Invalid trace id {trace_id!r}.")
        return self.root / f"{trace_id}.jsonl"

    def save(self, trace_id: str, records: list[EventRecord]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(trace_id)
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record.to_dict(), default=str) + "\n")
        return path

    def load(self, ident: str) -> TaskTrace:
        """By trace id (``tr_…``), task id (``task_…``) or a unique prefix."""
        if not self.root.is_dir():
            raise NotFoundError("No task traces yet.", hint="Traces are written by `highhx agent loop`, `browser replay` and benchmarks.")
        path = self.root / f"{ident}.jsonl"
        if not path.is_file():
            path = next((p for p in self.root.glob("*.jsonl") if p.stem.startswith(ident)), None) or next(
                (p for p in self.root.glob("*.jsonl") if f'"task_id": "{ident}"' in p.read_text(encoding="utf-8")), Path()
            )
        if not path.is_file():
            raise NotFoundError(f"No task trace {ident!r}.", hint="See `highhx trace list`.")
        records: list[EventRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            data = json.loads(line)
            records.append(
                EventRecord(
                    int(data.get("seq", 0)),
                    str(data.get("event", "")),
                    str(data.get("ts", "")),
                    dict(data.get("payload") or {}),
                    **{k: str(data.get(k, "")) for k in ("trace_id", "session_id", "task_id", "step_id", "action_id", "execution_id", "source")},
                )
            )
        return TaskTrace(path.stem, records)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(self.root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            trace = self.load(path.stem)
            out.append(
                {
                    "trace_id": trace.trace_id,
                    "task_id": trace.task_id,
                    "goal": trace.goal,
                    "status": trace.status,
                    "events": len(trace.records),
                    "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)),
                }
            )
        return out


class TaskTraceRecorder:
    """Records every event of the traces it sees and saves each one when its task ends (and on
    :meth:`flush`). Payloads are redacted before they are kept."""

    def __init__(self, bus: EventBus, store: TraceStore, *, redactor: Redactor | None = None) -> None:
        self.store = store
        self.recorder = EventRecorder.attach(bus, capacity=50_000, redactor=redactor)
        self.recorder.listen(self._on)
        self.saved: list[str] = []

    def _on(self, record: EventRecord) -> None:
        if record.name in ("task.completed", "task.failed") and record.trace_id:
            self.save(record.trace_id)

    def save(self, trace_id: str) -> Path:
        path = self.store.save(trace_id, self.recorder.for_trace(trace_id))
        if trace_id not in self.saved:
            self.saved.append(trace_id)
        return path

    def flush(self) -> None:
        for trace_id in {r.trace_id for r in self.recorder.snapshot() if r.trace_id}:
            self.save(trace_id)

    def close(self) -> None:
        self.flush()
        self.recorder.close()
