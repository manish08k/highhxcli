"""Structured results shared by every layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    """Lifecycle / outcome status of commands, steps and workflows."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

    @property
    def finished(self) -> bool:
        return self not in (Status.PENDING, Status.RUNNING)

    @property
    def ok(self) -> bool:
        return self in (Status.SUCCESS, Status.SKIPPED)


@dataclass
class CommandResult:
    """Outcome of executing a single command (possibly after retries)."""

    command: str
    exit_code: int | None
    status: Status
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    attempts: int = 1
    started_at: str | None = None
    error: str | None = None
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.status == Status.SUCCESS or (self.dry_run and self.status == Status.SKIPPED)

    def to_dict(self, *, include_output: bool = True) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        if not include_output:
            data.pop("stdout")
            data.pop("stderr")
        return data


@dataclass
class StepResult:
    """Outcome of a workflow step."""

    step_id: str
    status: Status
    commands: list[CommandResult] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    message: str = ""
    duration: float = 0.0
    allowed_failure: bool = False

    @property
    def exit_code(self) -> int | None:
        for result in reversed(self.commands):
            if result.exit_code is not None:
                return result.exit_code
        return None

    @property
    def effective_success(self) -> bool:
        """True if dependents may treat this step as successful."""
        return self.status == Status.SUCCESS or (self.status == Status.FAILED and self.allowed_failure)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.step_id,
            "status": str(self.status),
            "exit_code": self.exit_code,
            "duration": round(self.duration, 3),
            "message": self.message,
            "outputs": self.outputs,
            "allowed_failure": self.allowed_failure,
            "commands": [c.to_dict(include_output=False) for c in self.commands],
        }


@dataclass
class WorkflowResult:
    """Outcome of an entire workflow run."""

    workflow: str
    execution_id: str
    status: Status
    steps: dict[str, StepResult] = field(default_factory=dict)
    duration: float = 0.0
    outputs: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (Status.SUCCESS, Status.SKIPPED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "execution_id": self.execution_id,
            "status": str(self.status),
            "duration": round(self.duration, 3),
            "dry_run": self.dry_run,
            "outputs": self.outputs,
            "steps": [step.to_dict() for step in self.steps.values()],
        }


class CheckStatus(StrEnum):
    """Status of a diagnostic / validation check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class CheckResult:
    """A single diagnostic check outcome (doctor, env check, preflight …)."""

    name: str
    status: CheckStatus
    message: str = ""
    hint: str | None = None
    category: str = "general"
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": str(self.status),
            "message": self.message,
            "hint": self.hint,
            "category": self.category,
            "data": self.data,
        }


def summarize_checks(checks: list[CheckResult]) -> dict[str, int]:
    """Count checks per status."""
    counts = {status.value: 0 for status in CheckStatus}
    for check in checks:
        counts[check.status.value] += 1
    return counts
