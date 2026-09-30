"""The built-in engine's platform backends: the bridge protocol on each operating system.

    macos     CoreGraphics / Accessibility (ctypes) and System Events (JXA via osascript)
    windows   user32 (ctypes) and UI Automation (fixed PowerShell scripts)
    linux     X11 (xdotool), AT-SPI (when PyGObject is installed) and the desktop's screenshot tools

A backend implements ``op_<name>`` for the operations its platform can really perform; any
other operation is a structured ``unsupported_platform`` error naming what is missing — an
operation is never reported as done when it was not. ``capabilities`` says, feature by
feature, what is available here and why not.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar

from highhx.automation.engine.bridge import EngineError, Runner
from highhx.automation.engine.protocol import FEATURES, PROTOCOL_VERSION
from highhx.execution.cancellation import CancellationToken

T = TypeVar("T")


def png_size(path: Path) -> tuple[int, int]:
    """A PNG's width and height, from its header (checks it is one)."""
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise EngineError("failed", f"{path} is not a PNG")
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def feature(available: bool, detail: str) -> dict[str, Any]:
    return {"available": bool(available), "detail": detail}


BOUNDS_TOLERANCE = 4
"""Points an element may have shifted and still be the element the caller observed."""

Candidate = tuple[T, str, str, Sequence[int] | None]
"""(the platform's handle, role, name, bounds) — in accessibility-tree order."""


def choose_element(
    candidates: Sequence[Candidate[T]],
    name: str,
    role: str | None,
    *,
    index: int | None = None,
    bounds: Sequence[int] | None = None,
    where: str = "",
) -> T:
    """The one element a ``click`` means — never a guess.

    Exact names (case-insensitive) win over partial ones. Several exact matches need ``index``
    (the occurrence the caller observed); without it they are ``ambiguous_target``, as are
    several partial matches. ``index`` past the end, or ``bounds`` that no longer match, mean
    the UI changed since it was observed: ``stale_target``, so the caller observes again rather
    than pressing a different control."""
    wanted = name.lower()
    pool = [c for c in candidates if role in (None, "any") or c[1] == role]
    exact = [c for c in pool if c[2].lower() == wanted]
    label = f"{role or 'element'} named {name!r}" + (f" in {where}" if where else "")
    if index is not None:
        if index >= len(exact):
            raise EngineError(
                "stale_target",
                f"The {label} that was observed is gone ({len(exact)} such element(s) now); the UI changed.",
                hint="Observe again and choose the element from the new observation.",
            )
        chosen = exact[index]
    elif len(exact) == 1:
        chosen = exact[0]
    elif exact:
        raise EngineError(
            "ambiguous_target", f"{len(exact)} elements are {label}; say which one (index) instead of guessing."
        )
    else:
        partial = [c for c in pool if wanted in c[2].lower()]
        if not partial:
            raise EngineError("not_found", f"No {label}.")
        if len(partial) > 1:
            names = ", ".join(repr(c[2]) for c in partial[:5])
            raise EngineError("ambiguous_target", f"{name!r} matches several elements ({names}); use the exact name.")
        chosen = partial[0]
    current = chosen[3]
    if (
        bounds is not None
        and current
        and any(abs(a - b) > BOUNDS_TOLERANCE for a, b in zip(current, bounds, strict=True))
    ):
        raise EngineError(
            "stale_target",
            f"The {label} moved from {list(bounds)} to {list(current)} since it was observed.",
            hint="Observe again and choose the element from the new observation.",
        )
    return chosen[0]


class Backend:
    platform = "unknown"

    def __init__(self, runner: Runner, *, cancel: CancellationToken | None = None) -> None:
        self.runner = runner
        self.cancel = cancel

    # ------------------------------------------------------------ plumbing
    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            raise EngineError(
                "unsupported_platform", f"{op} is not available on {self.platform} with the built-in engine."
            )
        result: dict[str, Any] = handler(**args)
        return result

    def run(self, argv: list[str], what: str) -> str:
        """A fixed argv through HighhX's command engine; its output, or an EngineError."""
        code, out, err = self.runner(argv, what)
        if code != 0:
            raise EngineError("failed", f"{what} failed: {(err or out).strip()[-300:] or f'exit code {code}'}")
        return out

    def sleep(self, seconds: float) -> None:
        if self.cancel is not None:
            self.cancel.wait(seconds)
        else:
            time.sleep(seconds)

    def poll(self, check: Any, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            if check():
                return True
            if time.monotonic() >= deadline:
                return False
            self.sleep(0.25)

    # ---------------------------------------------------------- operations
    def features(self) -> dict[str, dict[str, Any]]:
        return {name: feature(False, f"not implemented on {self.platform}") for name in FEATURES}

    def op_capabilities(self) -> dict[str, Any]:
        found = self.features()
        return {"platform": self.platform, "features": {name: found[name] for name in FEATURES}}

    def op_status(self) -> dict[str, Any]:
        found = self.features()
        ok = found["accessibility"]["available"] or found["pointer"]["available"]
        missing = [f"{name}: {v['detail']}" for name, v in found.items() if not v["available"]]
        detail = found["accessibility"]["detail"] if ok else "; ".join(missing[:3])
        return {
            "engine": "python",
            "protocol": PROTOCOL_VERSION,
            "platform": self.platform,
            "ok": ok,
            "accessibility": found["accessibility"]["available"],
            "detail": detail,
        }

    def op_wait(self, ms: int) -> dict[str, Any]:
        self.sleep(ms / 1000)
        return {"ms": ms}


def backend_for(runner: Runner, *, cancel: CancellationToken | None = None, platform: str | None = None) -> Backend:
    """The backend for this operating system (``sys.platform`` read at call time)."""
    name = platform or sys.platform
    if name == "darwin":
        from highhx.automation.engine.platforms.macos import MacBackend

        return MacBackend(runner, cancel=cancel)
    if name.startswith("win"):
        from highhx.automation.engine.platforms.windows import WindowsBackend

        return WindowsBackend(runner, cancel=cancel)
    if name.startswith("linux"):
        from highhx.automation.engine.platforms.linux import LinuxBackend

        return LinuxBackend(runner, cancel=cancel)
    return Backend(runner, cancel=cancel)
