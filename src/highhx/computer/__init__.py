"""Computer use: semantic UI automation shared by HighhX Free and Pro.

* Free — deterministic automation with known targets: ``highhx computer …``, flow
  files, and ``highhx do`` for plain requests that map to fixed actions.
* Pro — the AI agent observes the UI and chooses among the valid action candidates.

Both go through the same runtime (:mod:`~highhx.computer.runtime`) and the same
safety gate. Perception is accessibility-first: native accessibility (macOS),
browser DOM (Chromium family via DevTools), local OCR (tesseract); a vision model
is an optional plugin, never required.

The HighhX Computer API (:class:`~highhx.computer.driver.HighhXDriver`) observes and operates the
desktop through the automation bridge and the built-in engine's native platform backends.
"""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    # ``from highhx.computer import HighhXDriver`` without loading the engine for every submodule import
    if name == "HighhXDriver":
        from highhx.computer.driver import HighhXDriver

        return HighhXDriver
    raise AttributeError(name)
