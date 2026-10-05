"""Virtual machines through Lima and Tart, driven through their command lines (fake runners here:
neither tool is installed on the build machine) and the executor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from highhx.drivers.base import CapabilityError
from highhx.runtimes import vm as vms
from highhx.runtimes.base import ExecResult
from tests.unit.actions.conftest import executor_for  # noqa: F401
from tests.unit.agent.conftest import agent_project, make_app  # noqa: F401


class FakeLima:
    def __init__(self) -> None:
        self.vms: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> ExecResult:
        self.calls.append(argv)
        args = argv[1:]
        ok = ExecResult(0, "", "", 0.1)
        if args[:2] == ["list", "--json"]:
            return ExecResult(
                0,
                "\n".join(
                    json.dumps({"name": n, "status": v["status"], "cpus": v["cpus"], "memory": v["memory"]})
                    for n, v in self.vms.items()
                ),
                "",
                0.1,
            )
        if args[0] == "create":
            opts = dict(a.lstrip("-").split("=", 1) for a in args[1:-1])
            self.vms[opts["name"]] = {
                "status": "Stopped",
                "cpus": int(opts["cpus"]),
                "memory": int(float(opts["memory"]) * 1024**3),
                "snapshots": {},
                "files": [],
            }
            return ok
        if args[0] == "start":
            self.vms[args[-1]]["status"] = "Running"
            return ok
        if args[0] == "stop":
            self.vms[args[-1]]["status"] = "Stopped"
            return ok
        if args[:2] == ["delete", "--force"]:
            del self.vms[args[-1]]
            return ok
        if args[:2] == ["snapshot", "create"]:
            self.vms[args[2]]["snapshots"][args[3].split("=", 1)[1]] = list(self.vms[args[2]]["files"])
            return ok
        if args[:2] == ["snapshot", "apply"]:
            self.vms[args[2]]["files"] = list(self.vms[args[2]]["snapshots"][args[3].split("=", 1)[1]])
            return ok
        if args[0] == "shell":
            name, command = args[1], args[3:]
            if command[:2] == ["touch", "f"]:
                self.vms[name]["files"].append("f")
            return ExecResult(0, " ".join(command) + "\n", "", 0.1)
        return ExecResult(1, "", f"unexpected {args}", 0.1)


@pytest.fixture
def lima(monkeypatch: pytest.MonkeyPatch) -> FakeLima:
    fake = FakeLima()
    monkeypatch.setattr(vms.LimaBackend, "available", classmethod(lambda cls: True))
    monkeypatch.setattr(vms.TartBackend, "available", classmethod(lambda cls: False))
    monkeypatch.setattr(vms, "DEFAULT_RUNNER", fake)
    return fake


def test_the_full_lifecycle_through_the_executor(agent_project: Path, executor_for, lima: FakeLima) -> None:  # noqa: F811
    executor, ui = executor_for(agent_project)
    created = executor.run("vm.create", {"name": "dev", "cpus": 2, "memory_mb": 2048})
    assert created.ok and created.verified and ui.requests  # medium risk: asked
    assert [
        "limactl",
        "create",
        "--name=dev",
        "--cpus=2",
        "--memory=2",
        "--disk=30",
        "--tty=false",
        "template://default",
    ] in lima.calls
    assert executor.run("vm.start", {"name": "dev"}).output["state"] == "running"
    assert executor.run("vm.exec", {"name": "dev", "argv": ["touch", "f"]}).ok
    assert executor.run("vm.snapshot", {"name": "dev", "tag": "clean"}).ok
    lima.vms["dev"]["files"].append("junk")
    assert executor.run("vm.restore", {"name": "dev", "tag": "clean"}).ok and lima.vms["dev"]["files"] == ["f"]
    paused = executor.run("vm.pause", {"name": "dev"})
    assert not paused.ok and "cannot pause" in paused.error  # Lima has no suspend: refused, not faked
    assert executor.run("vm.stop", {"name": "dev"}).output["state"] == "stopped"
    stopped_exec = executor.run("vm.exec", {"name": "dev", "argv": ["true"]})
    assert not stopped_exec.ok and "not running" in stopped_exec.error
    listed = executor.run("vm.list", {}).output
    assert listed["backend"] == "lima" and listed["vms"][0]["name"] == "dev"
    ui.action_answers = [False]
    assert executor.run("vm.destroy", {"name": "dev"}).status == "denied" and "dev" in lima.vms
    assert executor.run("vm.destroy", {"name": "dev"}).ok and "dev" not in lima.vms


def test_commands_in_a_vm_are_classified(agent_project, executor_for, lima: FakeLima) -> None:  # noqa: F811
    executor, _ = executor_for(agent_project)
    assert (
        executor.plan("vm.exec", {"name": "dev", "command": "rm -rf /"}).decision.risk == 4
    )  # critical, inside a VM too
    assert executor.plan("vm.restore", {"name": "dev", "tag": "t"}).decision.risk == 3
    for bad in ("Dev", "../x", "a b"):
        result = executor.run("vm.start", {"name": bad})
        assert not result.ok


def test_tart_suspends_and_snapshots_by_cloning() -> None:
    calls: list[list[str]] = []
    spawned: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> ExecResult:
        calls.append(argv)
        if argv[1] == "list":
            return ExecResult(
                0,
                json.dumps(
                    [{"Name": "mac", "State": "suspended", "Source": "local"}, {"Name": "ghcr.io/x", "Source": "oci"}]
                ),
                "",
                0.1,
            )
        return ExecResult(0, "", "", 0.1)

    tart = vms.TartBackend(runner=runner, spawn=lambda argv: spawned.append(argv) or 1)
    tart.create("mac", "ghcr.io/cirruslabs/macos-sonoma-base:latest", vms.VMLimits(4, 8192, 60))
    assert ["tart", "clone", "ghcr.io/cirruslabs/macos-sonoma-base:latest", "mac"] in calls
    assert ["tart", "set", "mac", "--cpu", "4", "--memory", "8192", "--disk-size", "60"] in calls
    tart.start("mac")
    assert spawned == [["tart", "run", "--no-graphics", "mac"]]
    tart.pause("mac")
    tart.snapshot("mac", "before")
    tart.restore("mac", "before")
    assert ["tart", "suspend", "mac"] in calls and ["tart", "clone", "mac", "mac-snap-before"] in calls
    assert calls[-2:] == [["tart", "delete", "mac"], ["tart", "clone", "mac-snap-before", "mac"]]
    assert [v.name for v in tart.list()] == ["mac"]  # remote images are not VMs
    with pytest.raises(Exception, match="needs an image"):
        tart.create("mac2", "", vms.VMLimits())


def test_without_a_backend_vms_say_what_to_install(agent_project: Path, executor_for, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(vms.LimaBackend, "available", classmethod(lambda cls: False))
    monkeypatch.setattr(vms.TartBackend, "available", classmethod(lambda cls: False))
    executor, _ = executor_for(agent_project)
    listed = executor.run("vm.list", {})
    assert listed.ok and listed.output["available"] is False and "Lima" in listed.output["detail"]
    created = executor.run("vm.create", {"name": "dev"})
    assert not created.ok and "No virtual-machine backend" in created.error
    with pytest.raises(CapabilityError, match="No virtual-machine backend"):
        vms.VMRuntime("dev")


def _ssh_stub(directory: Path, body: str) -> None:
    import stat

    directory.mkdir(exist_ok=True)
    script = directory / "ssh"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


def test_remote_heartbeat(agent_project: Path, executor_for, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    import os

    bin_dir = tmp_path / "bin"
    _ssh_stub(bin_dir, 'echo "$@" > "$(dirname "$0")/args"; echo "highhx 0.9.0"')
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    executor, _ = executor_for(agent_project)
    ok = executor.run("remote.check", {"target": "ssh://me@build.example:2222"})
    assert ok.ok and ok.output["highhx"] == "0.9.0" and ok.output["desktop"] is True
    args = (bin_dir / "args").read_text()
    assert "BatchMode=yes" in args and "-p 2222" in args and args.strip().endswith("highhx --version")
    _ssh_stub(bin_dir, 'echo "ssh: connect to host build.example port 22: Connection refused" >&2; exit 255')
    down = executor.run("remote.check", {"target": "ssh://me@build.example"})
    assert not down.ok and down.output["reachable"] is False and "Connection refused" in down.error
    _ssh_stub(bin_dir, 'echo "sh: highhx: not found" >&2; exit 127')
    bare = executor.run("remote.check", {"target": "ssh://me@build.example"})
    assert bare.ok and bare.output["desktop"] is False and "not installed" in bare.output["error"]
