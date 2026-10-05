"""Best-of-N: run a task several times in independent environments and keep the best attempt.

    task ─┬─ attempt 1 (its own environment) ─┐
          ├─ attempt 2 ────────────────────────┼─ evaluator (score 0…1, from the environment) ─ best
          └─ attempt N ────────────────────────┘

Attempts must not share the world: a real desktop or browser cannot be rolled back between them,
so :class:`BestOfN` refuses an environment factory that does not declare itself isolated. The
built-in one (:func:`project_copies`) gives each attempt its own copy of the project.

Budgets keep it from multiplying cost blindly: at most ``attempts``, a wall-clock budget and a
token budget across all attempts, and it stops early once an attempt scores 1.0. Each attempt is a
normal agent run — every action through that environment's executor, policy and approvals — with
its own trajectory. The result names the winner, every attempt's score, and the winner's changes
as a unified diff, which is applied to the real project only by the person (never automatically).
"""

from __future__ import annotations

import contextlib
import difflib
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from highhx.agent.loop.model import AgentTask, LoopResult
from highhx.core.errors import UsageError

IGNORED = shutil.ignore_patterns(
    ".git", ".highhx", "node_modules", ".venv", "venv", "__pycache__", ".env*", "*.pem", "*.key"
)


@dataclass
class AttemptEnvironment:
    executor: Any
    root: Path
    cleanup: Callable[[], None]


class EnvironmentFactory(Protocol):
    isolated: bool

    def __call__(self, attempt: int) -> AttemptEnvironment: ...


@dataclass
class Attempt:
    number: int
    status: str
    summary: str
    score: float
    steps: int
    tokens: int
    seconds: float
    task_id: str
    root: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in self.__dict__.items() if k != "root"}


@dataclass
class BestResult:
    best: Attempt | None
    attempts: list[Attempt] = field(default_factory=list)
    stopped: str = ""
    """Why no more attempts ran: ``perfect`` · ``attempts`` · ``time budget`` · ``token budget``."""
    diff: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "best": self.best.to_dict() if self.best else None,
            "attempts": [a.to_dict() for a in self.attempts],
            "stopped": self.stopped,
            "diff": self.diff,
        }


def default_evaluator(task: AgentTask, result: LoopResult, env: AttemptEnvironment) -> float:
    """1.0 for a completed run whose success check holds in its environment (checked again here,
    not taken from the agent), 0.5 for completed without a check, 0 otherwise."""
    if str(result.status) != "completed":
        return 0.0
    if task.success is None:
        return 0.5
    from highhx.verification.declarative import Verdict, VerificationContext, verify

    report = verify(task.success, VerificationContext(root=env.root))
    return 1.0 if report.verdict == Verdict.SATISFIED else 0.0


class BestOfN:
    def __init__(
        self,
        environments: EnvironmentFactory,
        planner_for: Callable[[int], Any],
        *,
        attempts: int = 3,
        seconds: float = 1800.0,
        tokens: int | None = None,
        evaluator: Callable[[AgentTask, LoopResult, AttemptEnvironment], float] = default_evaluator,
    ) -> None:
        if not getattr(environments, "isolated", False):
            raise UsageError(
                "Best-of-N needs isolated attempts (a real desktop or browser cannot be rolled back between them)."
            )
        if not 1 <= attempts <= 10:
            raise UsageError("attempts must be between 1 and 10")
        self.environments = environments
        self.planner_for = planner_for
        self.max_attempts = attempts
        self.seconds = seconds
        self.tokens = tokens
        self.evaluator = evaluator

    def run(self, task: AgentTask) -> BestResult:
        from highhx.agent.loop.loop import AgentLoop

        started = time.monotonic()
        tokens_used = 0
        out = BestResult(None)
        keep: AttemptEnvironment | None = None
        try:
            for number in range(1, self.max_attempts + 1):
                if time.monotonic() - started >= self.seconds:
                    out.stopped = "time budget"
                    break
                if self.tokens is not None and tokens_used >= self.tokens:
                    out.stopped = "token budget"
                    break
                env = self.environments(number)
                try:
                    child = AgentTask.from_dict(task.to_dict())
                    child.timeout = max(1.0, min(task.timeout, self.seconds - (time.monotonic() - started)))
                    result = AgentLoop(
                        env.executor, self.planner_for(number), memory=False, agent=f"attempt-{number}"
                    ).run(child)
                    score = float(self.evaluator(task, result, env))
                except Exception:
                    env.cleanup()
                    raise
                m = result.metrics
                tokens = int(m.get("tokens_in") or 0) + int(m.get("tokens_out") or 0)
                tokens_used += tokens
                attempt = Attempt(
                    number,
                    str(result.status),
                    result.summary,
                    round(score, 4),
                    int(m.get("steps") or 0),
                    tokens,
                    float(m.get("seconds") or 0.0),
                    result.trajectory.id,
                    env.root,
                )
                out.attempts.append(attempt)
                env.executor.events.emit(
                    "attempt.completed", attempt=number, score=attempt.score, status=attempt.status
                )
                if out.best is None or (attempt.score, -attempt.steps) > (out.best.score, -out.best.steps):
                    if keep is not None:
                        keep.cleanup()
                    out.best, keep = attempt, env
                else:
                    env.cleanup()
                if attempt.score >= 1.0:
                    out.stopped = "perfect"
                    break
            else:
                out.stopped = "attempts"
            if keep is not None and getattr(self.environments, "original", None) is not None:
                out.diff = project_diff(self.environments.original, keep.root)  # type: ignore[attr-defined]
        finally:
            if keep is not None:
                keep.cleanup()
        return out


def project_diff(original: Path, attempt: Path, *, limit: int = 200_000) -> str:
    """The attempt's changes to the project as a unified diff (text files; secrets never copied)."""
    chunks: list[str] = []
    names = {p.relative_to(attempt) for p in attempt.rglob("*") if p.is_file()} | {
        p.relative_to(original) for p in _copyable(original)
    }
    for rel in sorted(names):
        before_path, after_path = original / rel, attempt / rel
        try:
            before = before_path.read_text(encoding="utf-8").splitlines(keepends=True) if before_path.is_file() else []
            after = after_path.read_text(encoding="utf-8").splitlines(keepends=True) if after_path.is_file() else []
        except UnicodeDecodeError:
            continue
        if before != after:
            chunks.extend(difflib.unified_diff(before, after, f"a/{rel}", f"b/{rel}"))
        if sum(len(c) for c in chunks) > limit:
            chunks.append("… (diff truncated)\n")
            break
    return "".join(chunks)


def _copyable(root: Path) -> Iterator[Path]:
    skip = {".git", ".highhx", "node_modules", ".venv", "venv", "__pycache__"}
    for path in root.rglob("*"):
        if (
            path.is_file()
            and not (set(path.relative_to(root).parts) & skip)
            and not path.name.startswith(".env")
            and path.suffix not in (".pem", ".key")
        ):
            yield path


class project_copies:
    """Each attempt gets a fresh copy of the project (secrets and heavy folders left out) and its
    own HighhX app and executor, so attempts never see each other's changes."""

    isolated = True

    def __init__(self, app: Any, *, approvals: Any = None) -> None:
        self.app = app
        self.original = Path(app.root)
        self.approvals = approvals

    def __call__(self, attempt: int) -> AttemptEnvironment:
        from highhx.actions.executor import ActionExecutor
        from highhx.commands import App
        from highhx.core.context import Options
        from highhx.safety.actions import Actor
        from highhx.safety.gate import ActionGate, ApprovalMode

        base = Path(tempfile.mkdtemp(prefix=f"highhx-attempt-{attempt}-"))
        root = base / self.original.name
        shutil.copytree(self.original, root, ignore=IGNORED, symlinks=False)
        copy = App(Options(interactive=False, yes=self.app.options.yes), cwd=root)
        prompter = self.approvals if self.approvals is not None else _NoPrompts()
        gate = ActionGate(
            copy.engine, prompter, source=f"attempt-{attempt}", mode=ApprovalMode.ASK, assume_yes=self.app.options.yes
        )
        executor = ActionExecutor(copy, gate, actor=Actor.AGENT)

        def cleanup() -> None:
            with contextlib.suppress(Exception):
                executor.close()
                copy.close()
            shutil.rmtree(base, ignore_errors=True)

        return AttemptEnvironment(executor, root, cleanup)


class _NoPrompts:
    """Without a person to ask, anything that needs approval is refused (the attempt fails there)."""

    interactive = False

    def ask_permission(self, action: str, details: Any, *, allow_always: bool = True) -> str:
        return "no"

    def confirm_action(self, request: Any) -> bool:
        return False
