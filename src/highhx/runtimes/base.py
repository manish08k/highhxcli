"""Runtime: where an agent's work happens. One interface for every environment.

    Runtime
      info()           kind, id, workspace, isolation, network policy, limits
      capabilities()   what it can do here (exec, desktop driver …), with reasons
      exec(argv)       run a command inside it (timeout, limits, kill on cancel)
      driver()         the ComputerDriver for its screen, when it has one
      stop()           end it and clean up

    LocalRuntime     this computer, the project as it is (no isolation, the default)
    SandboxRuntime   a temporary workspace with filesystem, network, environment and resource
                     isolation (runtimes/sandbox.py)
    RemoteRuntime    another computer over SSH
    VMRuntime · CloudRuntime
                     not implemented: they report a capability error, never pretend

The agent loop does not care which runtime it runs in. Catalog actions reach a runtime only
after the executor approved them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from highhx.computer.providers import Capability
from highhx.drivers.base import CapabilityError

if TYPE_CHECKING:
    from highhx.drivers.base import ComputerDriver
    from highhx.execution.cancellation import CancellationToken

NETWORK_POLICIES = ("deny", "allow")


@dataclass(frozen=True)
class ResourceLimits:
    timeout: float = 600.0
    """Wall-clock seconds per command (the process group is killed after it)."""
    cpu_seconds: int = 600
    file_mb: int = 1024
    """Largest file a command may write."""
    memory_mb: int = 0
    """Address-space limit (0: none; enforced on Linux and by Docker)."""
    processes: int = 0
    """Process limit (0: none; enforced by Docker)."""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourceLimits:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "seconds": round(self.seconds, 3),
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class RuntimeInfo:
    kind: str
    id: str
    workspace: str = ""
    isolation: str = "none"
    """none · seatbelt · bubblewrap · docker · workspace (no filesystem confinement)"""
    network: str = "allow"
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "workspace": self.workspace,
            "isolation": self.isolation,
            "network": self.network,
            "limits": self.limits.to_dict(),
            **self.details,
        }


@runtime_checkable
class Runtime(Protocol):
    kind: str

    def info(self) -> RuntimeInfo: ...

    def capabilities(self) -> list[Capability]: ...

    def exec(
        self, argv: list[str], *, timeout: float | None = None, cancel: CancellationToken | None = None
    ) -> ExecResult: ...

    def driver(self) -> ComputerDriver: ...

    def stop(self) -> None: ...


class LocalRuntime:
    """This computer and the project as it is. Commands go through the engine (classified and
    approved by the executor); this object only describes the environment."""

    kind = "local"

    def __init__(self, root: Path, *, driver_factory: Any = None) -> None:
        self.root = root
        self._driver_factory = driver_factory

    def info(self) -> RuntimeInfo:
        return RuntimeInfo("local", "local", str(self.root), "none", "allow")

    def capabilities(self) -> list[Capability]:
        return [
            Capability("exec", True, "commands run in the project through the HighhX engine"),
            Capability("desktop", self._driver_factory is not None, "this computer's desktop"),
        ]

    def exec(self, argv: list[str], *, timeout: float | None = None, cancel: CancellationToken | None = None) -> ExecResult:
        from highhx.runtimes.process import run_process

        return run_process(argv, cwd=self.root, timeout=timeout or 600.0, cancel=cancel)

    def driver(self) -> ComputerDriver:
        if self._driver_factory is None:
            raise CapabilityError("No desktop driver was configured for this runtime.")
        driver: ComputerDriver = self._driver_factory()
        return driver

    def stop(self) -> None:
        pass


class _Unavailable:
    kind = "unavailable"
    detail = ""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise CapabilityError(f"The {self.kind} runtime is not implemented in this build: {self.detail}")


class VMRuntime(_Unavailable):
    kind = "vm"
    detail = "no hypervisor backend; use `highhx sandbox` for isolation or an ssh:// computer"


class CloudRuntime(_Unavailable):
    kind = "cloud"
    detail = "no cloud-computer backend; use `highhx sandbox` locally or an ssh:// computer"
