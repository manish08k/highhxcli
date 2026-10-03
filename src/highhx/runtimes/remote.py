"""RemoteRuntime: another computer over SSH. Commands run with ``ssh -T -o BatchMode=yes``
(keys only, never a password prompt). Its screen is operated through the existing remote engine
(``RemoteDriver``)."""

from __future__ import annotations

import shlex
import shutil
from typing import TYPE_CHECKING, Any

from highhx.computer.providers import Capability
from highhx.core.errors import UsageError
from highhx.drivers.base import CapabilityError
from highhx.runtimes.base import ExecResult, RuntimeInfo
from highhx.runtimes.process import run_process

if TYPE_CHECKING:
    from pathlib import Path

    from highhx.drivers.base import ComputerDriver
    from highhx.execution.cancellation import CancellationToken


class RemoteRuntime:
    kind = "remote"

    def __init__(self, target: str, *, cwd: Path, driver_factory: Any = None) -> None:
        from highhx.automation.engine.remote import parse_target

        if not target.startswith("ssh://"):
            raise UsageError(f"A remote runtime needs an ssh:// target, not {target!r}.")
        self.target = parse_target(target)
        self.url = target
        self.cwd = cwd
        self._driver_factory = driver_factory

    def info(self) -> RuntimeInfo:
        return RuntimeInfo("remote", self.url, "", "remote computer", "remote")

    def capabilities(self) -> list[Capability]:
        ssh = shutil.which("ssh")
        return [
            Capability("exec", bool(ssh), ssh or "ssh is not installed"),
            Capability("desktop", self._driver_factory is not None, "through `highhx computer engine` on the remote computer"),
        ]

    def _ssh(self) -> list[str]:
        argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30"]
        if self.target.port:
            argv += ["-p", str(self.target.port)]
        if self.target.known_hosts:
            argv += ["-o", f"UserKnownHostsFile={self.target.known_hosts}", "-o", "StrictHostKeyChecking=yes"]
        return [*argv, self.target.destination]

    def exec(self, argv: list[str], *, timeout: float | None = None, cancel: CancellationToken | None = None) -> ExecResult:
        if not shutil.which("ssh"):
            raise CapabilityError("ssh is not installed.")
        return run_process([*self._ssh(), shlex.join(argv)], cwd=self.cwd, timeout=timeout or 600.0, cancel=cancel)

    def driver(self) -> ComputerDriver:
        if self._driver_factory is None:
            raise CapabilityError("No remote desktop driver was configured.")
        driver: ComputerDriver = self._driver_factory()
        return driver

    def stop(self) -> None:
        pass
