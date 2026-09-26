"""Port inspection helpers."""

from __future__ import annotations

import re
import socket
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

from highhx.utils.platform import IS_WINDOWS
from highhx.utils.processes import which


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """True if something accepts TCP connections on ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """True if we could bind ``host:port`` right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if not IS_WINDOWS:
            # Ignore TIME_WAIT leftovers, like servers do; a listening socket still blocks the bind.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


@dataclass
class PortOwner:
    port: int
    pid: int | None
    process: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def port_owner(port: int) -> PortOwner:
    """Best-effort lookup of the process listening on ``port`` (lsof / netstat)."""
    try:
        if IS_WINDOWS:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10, check=False
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].rsplit(":", 1)[-1] == str(port):
                    return PortOwner(port, int(parts[4]), None)
            return PortOwner(port, None, None)
        if which("lsof"):
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
            pid = re.search(r"^p(\d+)", out, re.MULTILINE)
            name = re.search(r"^c(.+)$", out, re.MULTILINE)
            return PortOwner(port, int(pid.group(1)) if pid else None, name.group(1) if name else None)
        if which("ss"):
            out = subprocess.run(
                ["ss", "-ltnp", f"sport = :{port}"], capture_output=True, text=True, timeout=10, check=False
            ).stdout
            match = re.search(r'users:\(\("([^"]+)",pid=(\d+)', out)
            if match:
                return PortOwner(port, int(match.group(2)), match.group(1))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return PortOwner(port, None, None)
