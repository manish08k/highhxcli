"""Exception hierarchy and exit codes.

Every error raised intentionally by HighhX derives from :class:`HighhXError`,
carries an exit code, and optionally a *hint* telling the user what to do next.
The CLI renders these without a traceback (unless ``--debug`` is used).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import IntEnum


class ExitCode(IntEnum):
    """Process exit codes used by the CLI."""

    OK = 0
    FAILURE = 1
    USAGE = 2
    CONFIG = 3
    NOT_FOUND = 4
    MISSING_TOOL = 5
    APPROVAL_DENIED = 6
    POLICY_VIOLATION = 7
    VALIDATION = 8
    FINDINGS = 9
    TIMEOUT = 124
    COMMAND_NOT_FOUND = 127
    CANCELLED = 130


class HighhXError(Exception):
    """Base class for all expected HighhX errors."""

    exit_code: ExitCode = ExitCode.FAILURE
    category: str = "error"

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: Sequence[str] | None = None,
        exit_code: ExitCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = list(details or [])
        if exit_code is not None:
            self.exit_code = exit_code

    def to_dict(self) -> dict[str, object]:
        """JSON-friendly representation."""
        return {
            "error": self.category,
            "message": self.message,
            "hint": self.hint,
            "details": self.details,
            "exit_code": int(self.exit_code),
        }


class UsageError(HighhXError):
    """The command was invoked incorrectly."""

    exit_code = ExitCode.USAGE
    category = "usage"


class ConfigError(HighhXError):
    """Configuration file missing or invalid."""

    exit_code = ExitCode.CONFIG
    category = "config"


class ValidationError(HighhXError):
    """Input failed validation; ``details`` lists every problem."""

    exit_code = ExitCode.VALIDATION
    category = "validation"


class NotFoundError(HighhXError):
    """A requested object (workflow, target, execution …) does not exist."""

    exit_code = ExitCode.NOT_FOUND
    category = "not_found"


class ProjectNotInitializedError(HighhXError):
    """The current directory is not a HighhX project."""

    exit_code = ExitCode.CONFIG
    category = "not_initialized"

    def __init__(self, message: str = "This directory is not a HighhX project.") -> None:
        super().__init__(message, hint="Run `highhx init` in your project root first.")


class ToolNotFoundError(HighhXError):
    """A required external tool is not installed or not on PATH."""

    exit_code = ExitCode.MISSING_TOOL
    category = "missing_tool"

    def __init__(self, tool: str, *, purpose: str | None = None, hint: str | None = None) -> None:
        what = f" (needed to {purpose})" if purpose else ""
        super().__init__(
            f"`{tool}` is not installed or not on PATH{what}.",
            hint=hint or f"Install {tool} and retry, or run `highhx doctor` for a full check.",
        )
        self.tool = tool


class ExecutionError(HighhXError):
    """A command or step failed."""

    category = "execution"


class CommandFailedError(ExecutionError):
    """A subprocess exited unsuccessfully."""

    def __init__(self, command: str, exit_code: int, *, hint: str | None = None, stderr: str = "") -> None:
        details = list(stderr.strip().splitlines()[-10:]) if stderr else []
        super().__init__(f"Command failed with exit code {exit_code}: {command}", hint=hint, details=details)
        self.command = command
        self.command_exit_code = exit_code


class TimeoutExpiredError(ExecutionError):
    """An operation exceeded its timeout."""

    exit_code = ExitCode.TIMEOUT
    category = "timeout"


class OperationCancelledError(ExecutionError):
    """The operation was cancelled (Ctrl+C, SIGTERM or fail-fast)."""

    exit_code = ExitCode.CANCELLED
    category = "cancelled"


class ApprovalDeniedError(HighhXError):
    """A risky action was not approved."""

    exit_code = ExitCode.APPROVAL_DENIED
    category = "approval_denied"


class PolicyViolationError(HighhXError):
    """A project policy forbids the action."""

    exit_code = ExitCode.POLICY_VIOLATION
    category = "policy"


class WorkflowError(HighhXError):
    """Workflow definition or execution problem."""

    exit_code = ExitCode.VALIDATION
    category = "workflow"


class DependencyCycleError(WorkflowError):
    """The workflow dependency graph contains a cycle."""

    def __init__(self, cycle: Sequence[str]) -> None:
        path = " -> ".join(cycle)
        super().__init__(f"Circular dependency detected: {path}", hint="Remove one of the depends_on edges.")
        self.cycle = list(cycle)


class PluginError(HighhXError):
    """Plugin manifest, installation or loading problem."""

    category = "plugin"


class IntegrationError(HighhXError):
    """An external integration (Docker, SSH, Kubernetes, database …) failed."""

    category = "integration"
