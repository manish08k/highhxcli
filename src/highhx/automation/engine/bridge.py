"""The automation bridge: HighhX's single door to OS-level UI automation.

    action handler ──► AutomationBridge.call(op, **args)
                          │  validate against the protocol (fixed ops, checked arguments)
                          │  refuse keyboard input into a terminal
                          ▼
                       engine: the C#/.NET engine (a subprocess speaking JSON lines), or the
                       built-in Python engine (macOS System Events / Accessibility via osascript)

Handlers never talk to osascript, System Events or the .NET engine directly. The engine is
chosen by ``HIGHHX_AUTOMATION_ENGINE`` (``python``, ``dotnet`` or a path to the engine
binary); by default the .NET engine is used when it is installed and answers the protocol
handshake, and the Python engine otherwise.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from highhx.automation.engine.protocol import OPS, PROTOCOL_VERSION, validate
from highhx.core.errors import IntegrationError
from highhx.execution.cancellation import CancellationToken

ENGINE_ENV = "HIGHHX_AUTOMATION_ENGINE"
BINARY_NAME = "highhx-automation"
TERMINAL_APPS = frozenset(
    {"terminal", "iterm", "iterm2", "warp", "alacritty", "kitty", "hyper", "wezterm", "ghostty", "tabby"}
)

Runner = Callable[[list[str], str], tuple[int, str, str]]
"""Runs a fixed argv (what, for errors) → (exit code, stdout, stderr)."""


class EngineError(IntegrationError):
    """An automation engine refused or failed an operation (``code`` is a protocol error code)."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.code = code


def accessibility_denied(detail: str = "") -> EngineError:
    return EngineError(
        "accessibility_denied",
        "macOS has not granted Accessibility access, so HighhX cannot control other applications."
        + (f" ({detail})" if detail else ""),
        hint="Allow your terminal (and the HighhX engine, if installed) in System Settings → Privacy & Security → "
        "Accessibility, then run the request again.",
    )


class Engine(Protocol):
    name: str

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]: ...

    def close(self) -> None: ...


def is_terminal(app: str) -> bool:
    low = app.lower()
    return any(name in low for name in TERMINAL_APPS)


class AutomationBridge:
    """Validated, guarded calls into one automation engine."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @property
    def name(self) -> str:
        return self.engine.name

    def call(self, op: str, **args: Any) -> dict[str, Any]:
        clean = validate(op, args)
        if OPS[op].keyboard:
            front = str(self.engine.call("frontmost", {}).get("app") or "")
            if is_terminal(front):
                raise EngineError(
                    "refused",
                    f"{front} is a terminal: HighhX never types or presses keys into a terminal. "
                    "Run shell commands with !command so they are classified and approved.",
                )
        return self.engine.call(op, clean)

    def close(self) -> None:
        self.engine.close()


# ------------------------------------------------------------------ selection
def engine_binary() -> Path | None:
    """The installed .NET engine, if any: $HIGHHX_AUTOMATION_ENGINE (a path), PATH, or the data dir."""
    configured = os.environ.get(ENGINE_ENV, "")
    if configured and configured not in ("python", "dotnet", "auto"):
        path = Path(configured).expanduser()
        return path if path.is_file() else None
    found = shutil.which(BINARY_NAME)
    if found:
        return Path(found)
    from highhx.utils.paths import user_data_dir

    suffix = ".exe" if sys.platform.startswith("win") else ""
    candidate = user_data_dir() / "engine" / f"{BINARY_NAME}{suffix}"
    return candidate if candidate.is_file() else None


def open_bridge(runner: Runner, *, cancel: CancellationToken | None = None) -> AutomationBridge:
    """The bridge to the configured engine (see the module docstring)."""
    from highhx.automation.engine.python_engine import PythonEngine

    choice = os.environ.get(ENGINE_ENV, "auto").strip() or "auto"
    if choice == "python":
        return AutomationBridge(PythonEngine(runner, cancel=cancel))
    binary = engine_binary()
    if binary is None:
        if choice != "auto":
            raise EngineError(
                "not_found",
                "The HighhX .NET automation engine is not installed.",
                hint=f"Build it from engine/dotnet (see its README) or unset {ENGINE_ENV}.",
            )
        return AutomationBridge(PythonEngine(runner, cancel=cancel))
    from highhx.automation.engine.dotnet_engine import DotnetEngine

    try:
        return AutomationBridge(DotnetEngine(binary, cancel=cancel))
    except EngineError:
        if choice != "auto":
            raise
        return AutomationBridge(PythonEngine(runner, cancel=cancel))  # a broken install must not block Free


def engine_status(runner: Runner) -> dict[str, Any]:
    """Which engine is in use and what it reports (``highhx computer status``)."""
    try:
        bridge = open_bridge(runner)
    except EngineError as exc:
        return {"engine": None, "ok": False, "detail": exc.message, "protocol": PROTOCOL_VERSION}
    try:
        status = bridge.call("status")
    except IntegrationError as exc:
        status = {"ok": False, "detail": exc.message}
    finally:
        bridge.close()
    return {"engine": bridge.name, "protocol": PROTOCOL_VERSION, "binary": str(engine_binary() or ""), **status}
