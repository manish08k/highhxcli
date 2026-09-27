"""Operating system information."""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import asdict, dataclass
from typing import Any

from highhx.utils.platform import is_ci, is_wsl, system_name


@dataclass(frozen=True)
class OSInfo:
    system: str
    release: str
    version: str
    machine: str
    python_version: str
    python_executable: str
    shell: str | None
    wsl: bool
    ci: bool
    cpu_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def description(self) -> str:
        pretty = {"macos": "macOS", "linux": "Linux", "windows": "Windows"}.get(self.system, self.system)
        suffix = " (WSL)" if self.wsl else ""
        return f"{pretty} {self.release} {self.machine}{suffix}"


def detect_os() -> OSInfo:
    release = platform.mac_ver()[0] if system_name() == "macos" else platform.release()
    return OSInfo(
        system=system_name(),
        release=release or platform.release(),
        version=platform.version(),
        machine=platform.machine(),
        python_version=platform.python_version(),
        python_executable=sys.executable,
        shell=os.environ.get("SHELL") or os.environ.get("COMSPEC"),  # nosec B604 - `shell` is a data field ($SHELL), not a subprocess argument
        wsl=is_wsl(),
        ci=is_ci(),
        cpu_count=os.cpu_count() or 1,
    )
