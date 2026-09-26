"""Execution context shared by the engine and domain services."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from highhx.core.events import EventBus
from highhx.execution.cancellation import CancellationToken


@dataclass
class Options:
    """Global CLI options that influence behaviour everywhere."""

    verbose: bool = False
    debug: bool = False
    quiet: bool = False
    json: bool = False
    dry_run: bool = False
    yes: bool = False
    force: bool = False
    no_color: bool = False
    profile: str | None = None
    interactive: bool | None = None

    def is_interactive(self) -> bool:
        """True when HighhX may prompt the user."""
        if self.interactive is not None:
            return self.interactive
        if self.json or os.environ.get("HIGHHX_NON_INTERACTIVE"):
            return False
        try:
            return sys.stdin.isatty() and sys.stdout.isatty()
        except (AttributeError, ValueError):
            return False


@dataclass
class ExecutionContext:
    """Per-invocation runtime state (no hidden globals)."""

    options: Options = field(default_factory=Options)
    cwd: Path = field(default_factory=Path.cwd)
    cancel: CancellationToken = field(default_factory=CancellationToken)
    events: EventBus = field(default_factory=EventBus)
    env: dict[str, str] = field(default_factory=dict)
    """Extra environment variables injected into every command (e.g. the active profile)."""
