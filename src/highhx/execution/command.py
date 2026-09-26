"""Command specifications."""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from highhx.execution.retry import RetryPolicy
from highhx.execution.shell import needs_shell, shell_argv
from highhx.utils.platform import IS_WINDOWS
from highhx.utils.processes import which

SELF_COMMAND = "highhx"


def split_command(command: str) -> list[str]:
    """Split a command string into argv using platform-appropriate rules."""
    if IS_WINDOWS:
        # shlex in POSIX mode mangles backslashes in Windows paths.
        parts = shlex.split(command, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    return shlex.split(command)


def join_command(argv: Sequence[str]) -> str:
    """Render argv as a copy-pasteable string for display."""
    if IS_WINDOWS:
        return subprocess.list2cmdline(list(argv))
    return shlex.join(argv)


def _self_argv() -> list[str]:
    """argv that invokes HighhX itself, even when the entry point is not on PATH."""
    if which(SELF_COMMAND):
        return [SELF_COMMAND]
    return [sys.executable, "-m", "highhx"]


@dataclass
class CommandSpec:
    """Everything needed to run one command.

    ``command`` is either a string (split or run through a shell when it uses
    shell syntax) or an argv sequence (never run through a shell).
    """

    command: str | Sequence[str]
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    shell: bool | None = None
    name: str | None = None
    interactive: bool = False
    inherit_env: bool = True
    env_base: Mapping[str, str] | None = None
    stdin_data: str | None = None

    def display(self) -> str:
        """Human-readable command string."""
        if isinstance(self.command, str):
            return self.command
        return join_command(self.command)

    def uses_shell(self) -> bool:
        if not isinstance(self.command, str):
            return False
        if self.shell is not None:
            return self.shell
        return needs_shell(self.command)

    def argv(self) -> list[str]:
        """Resolve the argv to execute."""
        if isinstance(self.command, str):
            text = self.command.strip()
            if not text:
                raise ValueError("empty command")
            if self.uses_shell():
                if text == SELF_COMMAND or text.startswith(SELF_COMMAND + " "):
                    if not which(SELF_COMMAND):
                        text = join_command(_self_argv()) + text[len(SELF_COMMAND) :]
                return shell_argv(text)
            argv = split_command(text)
        else:
            argv = [str(part) for part in self.command]
        if not argv:
            raise ValueError("empty command")
        if argv[0] == SELF_COMMAND:
            argv = [*_self_argv(), *argv[1:]]
        elif IS_WINDOWS:
            resolved = which(argv[0])
            if resolved:
                argv[0] = resolved
        return argv

    def program(self) -> str:
        """The executable name (first argv element) for display and tool checks."""
        if isinstance(self.command, str):
            try:
                parts = split_command(self.command)
            except ValueError:
                parts = self.command.split()
            return parts[0] if parts else ""
        return str(self.command[0]) if self.command else ""
