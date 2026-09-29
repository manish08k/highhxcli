"""The built-in automation engine: the bridge protocol, in process, on every platform.

It hands each operation to the backend for this operating system
(:mod:`highhx.automation.engine.platforms`): native OS APIs on macOS (CoreGraphics and the
Accessibility API through ctypes; System Events through fixed JXA scripts), Windows (user32
through ctypes; UI Automation through fixed PowerShell scripts) and Linux (X11 tools and
AT-SPI). Every process a backend starts is a fixed argv run through the ``runner`` HighhX
gives it (the command engine: logged, policy-checked, cancellable); user text only ever
travels as an argument, never inside a script's source.
"""

from __future__ import annotations

from typing import Any

from highhx.automation.engine.bridge import Runner
from highhx.automation.engine.protocol import PROTOCOL_VERSION
from highhx.execution.cancellation import CancellationToken


class PythonEngine:
    name = "python"
    protocol = PROTOCOL_VERSION

    def __init__(self, runner: Runner, *, cancel: CancellationToken | None = None) -> None:
        from highhx.automation.engine.platforms import backend_for

        self.backend = backend_for(runner, cancel=cancel)

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.backend.call(op, args)

    def close(self) -> None:
        return None
