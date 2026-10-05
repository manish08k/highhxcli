"""Virtual computers: whole machines HighhX can create, run, snapshot and throw away.

    create · start · pause · resume · stop · destroy · snapshot · restore · exec · health

Backends are the standard tools for each platform, driven through their command lines:

    Lima   (limactl)  Linux VMs on macOS and Linux (QEMU or Apple Virtualization); snapshots
    Tart   (tart)     macOS and Linux VMs on Apple silicon (Virtualization.framework);
                      suspend/resume; snapshots as clones

A VM is isolated by construction: its own filesystem and kernel, its own network stack (Lima's
user-mode network; Tart's NAT), CPU and memory limits set when it is created. Commands run inside
it with ``exec`` (``limactl shell`` / ``tart exec``). Its desktop is operated by installing HighhX
inside it and connecting as a remote computer (``ssh://…``, see REMOTE_COMPUTER.md) — the same
driver, executor and approvals as any computer.

What a backend cannot do is refused, not faked (Lima has no suspend). Without lima or tart
installed, :class:`VMRuntime` raises a capability error saying what to install.

**Status: implemented; validated only through the backends' command lines with a fake runner in
this build (neither tool is installed on the build machine).**
"""

from __future__ import annotations

import builtins
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from highhx.computer.providers import Capability
from highhx.core.errors import UsageError
from highhx.drivers.base import CapabilityError
from highhx.runtimes.base import ExecResult, ResourceLimits, RuntimeInfo

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_TAG = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,40}$")
Runner = Callable[[list[str], float], ExecResult]


def _run(argv: list[str], timeout: float) -> ExecResult:
    from highhx.runtimes.process import run_process

    return run_process(argv, cwd=Path.cwd(), timeout=timeout)


def check_name(name: str) -> str:
    if not _NAME.match(name):
        raise UsageError(f"Invalid VM name {name!r} (lower-case letters, digits and '-').")
    return name


@dataclass(frozen=True)
class VMInfo:
    name: str
    status: str
    """running · stopped · suspended · unknown"""
    backend: str
    cpus: int = 0
    memory_mb: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class VMBackend:
    name = ""
    binary = ""
    can_pause = False

    def __init__(self, runner: Runner | None = None, spawn: Callable[[list[str]], int] | None = None) -> None:
        self.runner = runner or _run
        self.spawn = spawn or _spawn

    @classmethod
    def available(cls) -> bool:
        return shutil.which(cls.binary) is not None

    def _call(self, *args: str, timeout: float = 600.0) -> ExecResult:
        result = self.runner([self.binary, *args], timeout)
        if not result.ok:
            raise CapabilityError(
                f"{self.binary} {' '.join(args[:2])} failed: {(result.stderr or result.stdout).strip()[:300]}"
            )
        return result

    # every backend implements these
    def list(self) -> builtins.list[VMInfo]:
        raise NotImplementedError

    def create(self, name: str, image: str, limits: VMLimits) -> None:
        raise NotImplementedError

    def start(self, name: str) -> None:
        raise NotImplementedError

    def stop(self, name: str) -> None:
        raise NotImplementedError

    def destroy(self, name: str) -> None:
        raise NotImplementedError

    def snapshot(self, name: str, tag: str) -> None:
        raise NotImplementedError

    def restore(self, name: str, tag: str) -> None:
        raise NotImplementedError

    def exec(self, name: str, argv: builtins.list[str], timeout: float) -> ExecResult:
        raise NotImplementedError

    def pause(self, name: str) -> None:
        raise CapabilityError(f"{self.name} cannot pause a VM; stop it instead (it keeps its disk).")

    def resume(self, name: str) -> None:
        raise CapabilityError(f"{self.name} cannot resume a paused VM; start it instead.")


@dataclass(frozen=True)
class VMLimits:
    cpus: int = 2
    memory_mb: int = 4096
    disk_gb: int = 30


def _spawn(argv: list[str]) -> int:
    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
    return process.pid


class LimaBackend(VMBackend):
    name = "lima"
    binary = "limactl"

    def list(self) -> builtins.list[VMInfo]:
        out = self._call("list", "--json", timeout=60).stdout
        infos = []
        for line in out.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            infos.append(
                VMInfo(
                    str(item.get("name")),
                    str(item.get("status", "unknown")).lower(),
                    self.name,
                    int(item.get("cpus") or 0),
                    int(item.get("memory") or 0) // (1024 * 1024),
                )
            )
        return infos

    def create(self, name: str, image: str, limits: VMLimits) -> None:
        self._call(
            "create",
            f"--name={check_name(name)}",
            f"--cpus={limits.cpus}",
            f"--memory={limits.memory_mb / 1024:g}",
            f"--disk={limits.disk_gb}",
            "--tty=false",
            image or "template://default",
        )

    def start(self, name: str) -> None:
        self._call("start", "--tty=false", check_name(name), timeout=900)

    def stop(self, name: str) -> None:
        self._call("stop", check_name(name), timeout=300)

    def destroy(self, name: str) -> None:
        self._call("delete", "--force", check_name(name), timeout=300)

    def snapshot(self, name: str, tag: str) -> None:
        self._call("snapshot", "create", check_name(name), f"--tag={_tag(tag)}", timeout=600)

    def restore(self, name: str, tag: str) -> None:
        self._call("snapshot", "apply", check_name(name), f"--tag={_tag(tag)}", timeout=600)

    def exec(self, name: str, argv: builtins.list[str], timeout: float) -> ExecResult:
        return self.runner([self.binary, "shell", check_name(name), "--", *argv], timeout)


class TartBackend(VMBackend):
    name = "tart"
    binary = "tart"
    can_pause = True

    def list(self) -> builtins.list[VMInfo]:
        out = self._call("list", "--format", "json", timeout=60).stdout
        try:
            items = json.loads(out or "[]")
        except ValueError:
            items = []
        return [
            VMInfo(str(i.get("Name")), str(i.get("State", "unknown")).lower(), self.name)
            for i in items
            if i.get("Source") == "local"
        ]

    def create(self, name: str, image: str, limits: VMLimits) -> None:
        if not image:
            raise UsageError("Tart needs an image to clone, e.g. ghcr.io/cirruslabs/macos-sonoma-base:latest.")
        self._call("clone", image, check_name(name), timeout=3600)
        self._call(
            "set",
            name,
            "--cpu",
            str(limits.cpus),
            "--memory",
            str(limits.memory_mb),
            "--disk-size",
            str(limits.disk_gb),
        )

    def start(self, name: str) -> None:
        self.spawn([self.binary, "run", "--no-graphics", check_name(name)])  # runs until stopped

    def stop(self, name: str) -> None:
        self._call("stop", check_name(name), timeout=300)

    def pause(self, name: str) -> None:
        self._call("suspend", check_name(name), timeout=300)

    def resume(self, name: str) -> None:
        self.start(name)  # tart resumes a suspended VM when it is run again

    def destroy(self, name: str) -> None:
        self._call("delete", check_name(name), timeout=300)

    def snapshot(self, name: str, tag: str) -> None:
        self._call("clone", check_name(name), f"{name}-snap-{_tag(tag)}", timeout=3600)

    def restore(self, name: str, tag: str) -> None:
        snap = f"{check_name(name)}-snap-{_tag(tag)}"
        self._call("delete", name, timeout=300)
        self._call("clone", snap, name, timeout=3600)

    def exec(self, name: str, argv: builtins.list[str], timeout: float) -> ExecResult:
        return self.runner([self.binary, "exec", check_name(name), *argv], timeout)


BACKENDS: dict[str, type[VMBackend]] = {"lima": LimaBackend, "tart": TartBackend}
DEFAULT_RUNNER: Runner | None = None
"""The command runner backends use (tests set a fake one)."""
DEFAULT_SPAWN: Callable[[list[str]], int] | None = None


def _tag(tag: str) -> str:
    if not _TAG.match(tag):
        raise UsageError(f"Invalid snapshot tag {tag!r}.")
    return tag


def available_vm_backends() -> dict[str, Capability]:
    return {
        name: Capability(name, cls.available(), shutil.which(cls.binary) or f"{cls.binary} is not installed")
        for name, cls in BACKENDS.items()
    }


class VMRuntime:
    """One VM as a :class:`~highhx.runtimes.base.Runtime`: exec runs inside it; its desktop is a
    remote computer once HighhX runs inside it."""

    kind = "vm"

    def __init__(
        self, name: str, *, backend: VMBackend | None = None, limits: ResourceLimits | None = None, ssh_target: str = ""
    ) -> None:
        if backend is None:
            chosen = next((cls for cls in BACKENDS.values() if cls.available()), None)
            if chosen is None:
                raise CapabilityError(
                    "No virtual-machine backend is installed (lima or tart).",
                    hint="Install Lima (brew install lima) or Tart (brew install cirruslabs/cli/tart), or use `highhx sandbox`.",
                )
            backend = chosen()
        self.name = check_name(name)
        self.backend = backend
        self.limits = limits or ResourceLimits()
        self.ssh_target = ssh_target

    def info(self) -> RuntimeInfo:
        return RuntimeInfo("vm", self.name, "", f"{self.backend.name} virtual machine", "vm network", self.limits)

    def capabilities(self) -> builtins.list[Capability]:
        return [
            Capability("exec", True, f"{self.backend.binary} {'shell' if self.backend.name == 'lima' else 'exec'}"),
            Capability(
                "pause", self.backend.can_pause, "" if self.backend.can_pause else f"{self.backend.name} cannot pause"
            ),
            Capability(
                "desktop",
                bool(self.ssh_target),
                self.ssh_target or "install HighhX in the VM and give its ssh:// target",
            ),
        ]

    def health(self) -> VMInfo:
        found = next((vm for vm in self.backend.list() if vm.name == self.name), None)
        return found or VMInfo(self.name, "missing", self.backend.name)

    def exec(self, argv: list[str], *, timeout: float | None = None, cancel: Any = None) -> ExecResult:
        if self.health().status != "running":
            raise CapabilityError(f"VM {self.name} is not running.", hint="Start it first.")
        return self.backend.exec(self.name, argv, timeout or self.limits.timeout)

    def driver(self) -> Any:
        if not self.ssh_target:
            raise CapabilityError(
                f"VM {self.name}'s desktop is reached through HighhX running inside it.",
                hint="Install HighhX in the VM and pass its ssh:// target.",
            )
        from highhx.runtimes.remote import RemoteRuntime

        return RemoteRuntime(self.ssh_target, cwd=Path.cwd()).driver()

    def stop(self) -> None:
        self.backend.stop(self.name)
