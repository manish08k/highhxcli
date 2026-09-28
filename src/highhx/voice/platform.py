"""Which machines local voice supports, and how dependencies are installed on each.

Voice input needs three local programs: an audio recorder, whisper.cpp's CLI and a model
file. This module only *describes* the machine (operating system, package manager); the
installing is done by :mod:`highhx.voice.installer` after the person agrees to it.
"""

from __future__ import annotations

import os
import platform as _platform
import shutil
from collections.abc import Callable
from dataclasses import dataclass

from highhx.utils.platform import system_name

Which = Callable[[str], "str | None"]

PACKAGE_MANAGERS = ("brew", "apt-get", "dnf", "pacman", "zypper")
"""Package managers HighhX can install the recorder (and on Homebrew, whisper.cpp) with."""


@dataclass(frozen=True)
class VoicePlatform:
    system: str
    """``macos``, ``linux``, ``windows`` or another ``sys.platform`` value."""
    machine: str
    package_manager: str | None
    wsl: bool = False

    @property
    def supported(self) -> bool:
        return self.system in ("macos", "linux") and not self.wsl

    @property
    def label(self) -> str:
        names = {"macos": "macOS", "linux": "Linux", "windows": "Windows"}
        return f"{names.get(self.system, self.system)} {self.machine}".strip() + (" (WSL)" if self.wsl else "")

    def unsupported_reason(self) -> str | None:
        if self.supported:
            return None
        if self.wsl:
            return "WSL has no direct microphone access; run HighhX in a native Linux, macOS terminal instead."
        if self.system == "windows":
            return "Voice input is not supported on Windows yet (spoken replies still work)."
        return f"Voice input is not supported on {self.system}."

    @property
    def needs_root(self) -> bool:
        """System package managers other than Homebrew install as root."""
        return self.package_manager not in (None, "brew") and hasattr(os, "geteuid") and os.geteuid() != 0


def detect(which: Which = shutil.which) -> VoicePlatform:
    from highhx.utils.platform import is_wsl

    system = system_name()
    manager = next((name for name in PACKAGE_MANAGERS if which(name)), None)
    if system == "macos" and manager != "brew":
        manager = None  # only Homebrew is used on macOS
    return VoicePlatform(system, _platform.machine(), manager, wsl=is_wsl())
