"""Trajectory memory: what the agent saw, did, got, checked and concluded, step by step.

    Trajectory
      task, surface, status, plan, metrics (actions, failures, recoveries, tokens, cost, seconds)
      steps:
        observation    fingerprint, URL/app, element count, screenshot reference (never pixels)
        action         the ActionRequest (typed text replaced by its length unless replayable)
        result         the ActionResponse (outcome, status, risk, approval, error)
        verification   the declarative report
        reflection     what the reflector decided and why
        grounding      every strategy tried for the target, and the target's selectors

Stored as redacted JSON files (one per task) in the project's ``.highhx/trajectories`` or, outside
a project, the user data directory. :meth:`TrajectoryStore.search` finds similar past tasks
(lexical embedding by default; any EmbeddingModel can replace it). :meth:`hints` returns the
selectors that found a target before, so grounding can start from what worked.
:func:`replay_steps` turns a trajectory into semantic steps (action type, target, parameters).
A replay re-grounds every target on the current screen and never repeats recorded coordinates
when a target is known.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from highhx.core.errors import NotFoundError, ValidationError
from highhx.core.events import new_id

if TYPE_CHECKING:
    from highhx.models.interfaces import EmbeddingModel
    from highhx.security.secrets import Redactor

SCHEMA = 1


@dataclass
class TrajectoryStep:
    index: int
    intent: str
    action: dict[str, Any]
    result: dict[str, Any] = field(default_factory=dict)
    observation: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] | None = None
    reflection: dict[str, Any] | None = None
    grounding: dict[str, Any] | None = None
    started: float = field(default_factory=time.time)
    seconds: float = 0.0

    @property
    def outcome(self) -> str:
        return str(self.result.get("outcome") or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "intent": self.intent,
            "action": self.action,
            "result": self.result,
            "observation": self.observation,
            "verification": self.verification,
            "reflection": self.reflection,
            "grounding": self.grounding,
            "started": self.started,
            "seconds": round(self.seconds, 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrajectoryStep:
        return cls(
            int(data.get("index", 0)),
            str(data.get("intent", "")),
            dict(data.get("action") or {}),
            dict(data.get("result") or {}),
            dict(data.get("observation") or {}),
            data.get("verification"),
            data.get("reflection"),
            data.get("grounding"),
            float(data.get("started") or 0.0),
            float(data.get("seconds") or 0.0),
        )


@dataclass
class Trajectory:
    task: str
    surface: str = ""
    id: str = field(default_factory=lambda: new_id("task"))
    trace_id: str = field(default_factory=lambda: new_id("tr"))
    status: str = "running"
    """running · completed · failed · needs_user · cancelled · interrupted"""
    plan: list[dict[str, Any]] = field(default_factory=list)
    steps: list[TrajectoryStep] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    started: float = field(default_factory=time.time)
    ended: float | None = None
    planner: str = ""
    agent: str = "agent"
    """Which agent ran it (``agent``, or a specialist's name)."""

    def add(self, step: TrajectoryStep) -> None:
        self.steps.append(step)

    def outcomes(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for step in self.steps:
            counts[step.outcome] = counts.get(step.outcome, 0) + 1
        return counts

    def text(self) -> str:
        """What is searched: the task and every step's intent and target."""
        parts = [self.task, self.summary]
        for step in self.steps:
            target = step.action.get("target") or {}
            parts += [step.intent, str(target.get("label") or "")]
        return " ".join(p for p in parts if p)

    def describe(self) -> str:
        lines = [f"{self.id}  {self.status}  {self.task}"]
        for step in self.steps:
            target = (step.action.get("target") or {}).get("label") or ""
            how = (step.grounding or {}).get("candidate", {}) or {}
            strategy = f" via {how.get('strategy')}" if how.get("strategy") else ""
            lines.append(
                f"  {step.index:>2}. {step.action.get('action_type', '?'):<18} {target!s:<24} "
                f"{step.outcome or '-':<15}{strategy}"
            )
            reflection = step.reflection or {}
            if reflection.get("decision") not in (None, "continue"):
                lines.append(f"      ↳ {reflection.get('decision')}: {reflection.get('reason', '')}")
        if self.summary:
            lines.append(f"  result: {self.summary}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "id": self.id,
            "trace_id": self.trace_id,
            "task": self.task,
            "surface": self.surface,
            "status": self.status,
            "plan": self.plan,
            "steps": [s.to_dict() for s in self.steps],
            "metrics": self.metrics,
            "summary": self.summary,
            "started": self.started,
            "ended": self.ended,
            "planner": self.planner,
            "agent": self.agent,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Trajectory:
        if int(data.get("schema", SCHEMA)) > SCHEMA:
            raise ValidationError("This trajectory was written by a newer HighhX.")
        return cls(
            task=str(data.get("task", "")),
            surface=str(data.get("surface", "")),
            id=str(data.get("id") or new_id("task")),
            trace_id=str(data.get("trace_id") or new_id("tr")),
            status=str(data.get("status", "running")),
            plan=list(data.get("plan") or []),
            steps=[TrajectoryStep.from_dict(s) for s in data.get("steps") or []],
            metrics=dict(data.get("metrics") or {}),
            summary=str(data.get("summary", "")),
            started=float(data.get("started") or 0.0),
            ended=data.get("ended"),
            planner=str(data.get("planner", "")),
            agent=str(data.get("agent", "agent")),
        )


@dataclass(frozen=True)
class SearchHit:
    trajectory: Trajectory
    score: float


class TrajectoryStore:
    def __init__(self, root: Path, *, redactor: Redactor | None = None, embedding: EmbeddingModel | None = None) -> None:
        self.root = root
        self.redactor = redactor
        self._embedding = embedding

    @classmethod
    def for_app(cls, app: Any) -> TrajectoryStore:
        from highhx.utils.paths import user_data_dir

        root = (app.root / ".highhx" / "trajectories") if getattr(app, "initialized", False) else user_data_dir() / "trajectories"
        return cls(root, redactor=app.redactor)

    @property
    def embedding(self) -> EmbeddingModel:
        if self._embedding is None:
            from highhx.models.adapters import HashingEmbedding

            self._embedding = HashingEmbedding()
        return self._embedding

    def path(self, trajectory_id: str) -> Path:
        if not trajectory_id.replace("_", "").isalnum():
            raise ValidationError(f"Invalid trajectory id {trajectory_id!r}.")
        return self.root / f"{trajectory_id}.json"

    # ------------------------------------------------------------- persistence
    def save(self, trajectory: Trajectory) -> Path:
        from highhx.utils.filesystem import atomic_write_text

        text = json.dumps(trajectory.to_dict(), indent=1, default=str, ensure_ascii=False)
        if self.redactor is not None:
            text = self.redactor.redact(text)
        path = self.path(trajectory.id)
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text)
        return path

    def load(self, trajectory_id: str) -> Trajectory:
        path = self.path(trajectory_id)
        if not path.is_file():
            matches = sorted(self.root.glob(f"{trajectory_id}*.json")) if self.root.is_dir() else []
            if len(matches) != 1:
                raise NotFoundError(f"No trajectory {trajectory_id!r}.", hint="See `highhx trajectories list`.")
            path = matches[0]
        return Trajectory.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def recent(self, *, limit: int = 50, status: str | None = None) -> list[Trajectory]:
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                item = Trajectory.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, ValidationError):
                continue
            if status and item.status != status:
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def delete(self, trajectory_id: str) -> None:
        self.path(trajectory_id).unlink(missing_ok=True)

    # ------------------------------------------------------------------ memory
    def search(self, query: str, *, limit: int = 5, status: str | None = None, min_score: float = 0.15) -> list[SearchHit]:
        from highhx.models.adapters import cosine

        items = self.recent(limit=500, status=status)
        if not items or not query.strip():
            return []
        vectors = self.embedding.embed([query, *(t.text() for t in items)])
        wanted, rest = vectors[0], vectors[1:]
        hits = [SearchHit(t, cosine(wanted, v)) for t, v in zip(items, rest, strict=True)]
        return sorted((h for h in hits if h.score >= min_score), key=lambda h: h.score, reverse=True)[:limit]

    def hints(self, label: str, *, url: str = "", app: str = "") -> list[dict[str, Any]]:
        """Targets (all their selectors) that grounded ``label`` successfully before, most recent
        first, preferring the same page or application."""
        found: list[tuple[int, float, dict[str, Any]]] = []
        wanted = " ".join(label.lower().split())
        for trajectory in self.recent(limit=200):
            for step in trajectory.steps:
                target = (step.grounding or {}).get("target") or {}
                if not target or step.outcome not in ("success", "partial_success"):
                    continue
                labels = {" ".join(str(target.get("label", "")).lower().split())}
                semantic = target.get("semantic") or {}
                labels.add(" ".join(str(semantic.get("label", "")).lower().split()))
                if wanted not in labels:
                    continue
                coordinate = target.get("coordinate") or {}
                same = int(bool(url) and coordinate.get("url", "").split("?")[0] == url.split("?", maxsplit=1)[0]) + int(
                    bool(app) and coordinate.get("app") == app
                )
                found.append((same, step.started, target))
        return [t for _same, _when, t in sorted(found, key=lambda x: (x[0], x[1]), reverse=True)]

    def lessons(self, query: str, *, limit: int = 3) -> list[str]:
        """Short notes from similar past tasks: what worked, and what failed and why."""
        notes: list[str] = []
        for hit in self.search(query, limit=limit):
            t = hit.trajectory
            worked = [s for s in t.steps if s.outcome == "success"]
            failed = [s for s in t.steps if s.outcome == "failed" and s.reflection]
            line = f"{t.status}: {t.task!r} in {len(t.steps)} step(s)"
            if worked:
                line += "; worked: " + ", ".join(f"{s.action.get('action_type')} {(s.action.get('target') or {}).get('label', '')}".strip() for s in worked[:6])
            if failed:
                line += "; failed: " + "; ".join(str((s.reflection or {}).get("reason", "")) for s in failed[:3])
            notes.append(line)
        return notes


def summarize(trajectory: Trajectory) -> dict[str, Any]:
    outcomes = trajectory.outcomes()
    recoveries = sum(1 for s in trajectory.steps if (s.reflection or {}).get("decision") in ("retry", "replan", "recover"))
    strategies: dict[str, int] = {}
    for step in trajectory.steps:
        strategy = ((step.grounding or {}).get("candidate") or {}).get("strategy")
        if strategy:
            strategies[strategy] = strategies.get(strategy, 0) + 1
    return {
        "id": trajectory.id,
        "task": trajectory.task,
        "status": trajectory.status,
        "steps": len(trajectory.steps),
        "outcomes": outcomes,
        "recoveries": recoveries,
        "grounding": strategies,
        "seconds": round((trajectory.ended or time.time()) - trajectory.started, 2),
        "metrics": trajectory.metrics,
        "summary": trajectory.summary,
    }


def replay_steps(trajectory: Trajectory, *, only_successful: bool = True) -> list[dict[str, Any]]:
    """Semantic steps for a :class:`~highhx.agent.loop.planner.ScriptedPlanner`: the action type,
    the target with all its selectors, and parameters. Coordinates are dropped whenever a target
    is known (the replay grounds it again on the current screen)."""
    steps: list[dict[str, Any]] = []
    for step in trajectory.steps:
        if only_successful and step.outcome not in ("success", "partial_success"):
            continue
        action = step.action
        semantic = action.get("step") or {}
        params = dict((semantic.get("parameters") if semantic else action.get("parameters")) or {})
        target = ((step.grounding or {}).get("target")) or semantic.get("target") or action.get("target") or {}
        if target.get("label"):
            for key in ("x", "y", "capture", "space"):
                params.pop(key, None)
        if any(isinstance(v, str) and v.startswith("<") and v.endswith(" characters>") for v in params.values()):
            params["__redacted__"] = True
        steps.append(
            {
                "action": semantic.get("action") or action.get("action_type"),
                "intent": step.intent,
                "target": target,
                "parameters": params,
                **({"verify": action.get("verification")} if action.get("verification") else {}),
            }
        )
    return steps
