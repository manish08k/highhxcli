"""The Android emulator lifecycle and AndroidWorld-style tasks (this phase), through fakes: the
emulator binary and adb are simulated, the client, handlers, executor, agent loop and benchmark
runner are real. Nothing here claims a real emulator ran (none is installed on the build machine)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from highhx.benchmarks import BenchmarkSuite
from highhx.benchmarks.environments.android import SETTINGS, FakeDevice
from highhx.benchmarks.environments.android_world import (
    AndroidTask,
    DeviceUnavailable,
    device_for,
    register,
    task_class,
)
from highhx.benchmarks.runner import BenchmarkRunner
from highhx.core.errors import UsageError
from highhx.drivers.android import adb as adb_module
from highhx.drivers.android.adb import AdbClient, AdbError
from highhx.drivers.android.emulator import Emulator


class FakeSDK:
    """``emulator -list-avds`` plus adb: the AVD's serial appears after start and boots after a while."""

    def __init__(self, boots_after: int = 2) -> None:
        self.started: list[list[str]] = []
        self.calls: list[list[str]] = []
        self.boot_polls = 0
        self.boots_after = boots_after

    def __call__(self, argv: list[str], timeout: float, cancel: Any) -> tuple[int, bytes, bytes]:
        self.calls.append(argv)
        if argv[1:] == ["-list-avds"]:
            return 0, b"Pixel_6_API_33\nAndroidWorldAvd\n", b""
        if argv[1:] == ["devices", "-l"]:
            line = "emulator-5554\tdevice product:sdk model:sdk transport_id:3\n" if self.started else ""
            return 0, ("List of devices attached\n" + line).encode(), b""
        if argv[1:] == ["-s", "emulator-5554", "shell", "getprop", "sys.boot_completed"]:
            self.boot_polls += 1
            return 0, b"1\n" if self.boot_polls >= self.boots_after else b"\n", b""
        if argv[1:] == ["-s", "emulator-5554", "emu", "kill"]:
            return 0, b"OK: killing emulator", b""
        return 1, b"", f"unexpected {argv}".encode()

    def spawn(self, argv: list[str], log: Path) -> int:
        self.started.append(argv)
        return 4242


def emulator(sdk: FakeSDK) -> Emulator:
    return Emulator("emulator", adb=AdbClient("adb", runner=sdk), runner=sdk, spawner=sdk.spawn, sleep=lambda _s: None)


def test_avds_are_listed_and_one_starts_headless_with_grpc(tmp_path: Path) -> None:
    sdk = FakeSDK()
    emu = emulator(sdk)
    assert emu.avds() == ["Pixel_6_API_33", "AndroidWorldAvd"]
    started = emu.start("AndroidWorldAvd", log=tmp_path / "emu.log", timeout=5)
    assert started == {
        "avd": "AndroidWorldAvd",
        "serial": "emulator-5554",
        "pid": 4242,
        "grpc_port": 8554,
        "log": str(tmp_path / "emu.log"),
    }
    (argv,) = sdk.started
    assert argv[:3] == ["emulator", "-avd", "AndroidWorldAvd"] and argv[3:5] == ["-grpc", "8554"]
    assert "-no-window" in argv and "-no-snapshot-save" in argv and "-wipe-data" not in argv
    assert sdk.boot_polls == 2  # waited until Android reported boot_completed
    emu.stop("emulator-5554")
    assert sdk.calls[-1][1:] == ["-s", "emulator-5554", "emu", "kill"]


def test_bad_names_unknown_avds_and_boot_timeouts(tmp_path: Path) -> None:
    sdk = FakeSDK(boots_after=10**9)
    emu = emulator(sdk)
    with pytest.raises(UsageError):
        emu.start("../../etc", log=tmp_path / "x.log")
    with pytest.raises(UsageError, match="No AVD named"):
        emu.start("Nexus_1", log=tmp_path / "x.log")
    with pytest.raises(AdbError, match="did not finish booting"):
        emu.start("Pixel_6_API_33", log=tmp_path / "x.log", timeout=0.05)
    with pytest.raises(UsageError):
        emu.stop("R58M123456")  # a phone, not an emulator: never `emu kill`
    assert not Emulator("", adb=AdbClient("adb", runner=sdk), runner=sdk).emulator


def test_emulator_actions_go_through_the_executor(
    agent_project: Path, make_app, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from highhx.actions.executor import ActionExecutor
    from highhx.computer.session import ComputerSession
    from highhx.safety.actions import Actor
    from highhx.safety.audit import AuditLog
    from highhx.safety.gate import ActionGate, ApprovalMode
    from tests.unit.agent.conftest import RecordingUI

    sdk = FakeSDK()
    monkeypatch.setattr(ComputerSession, "android_emulator", lambda self, cancel=None: emulator(sdk))
    monkeypatch.setattr("highhx.utils.paths.user_data_dir", lambda: tmp_path)
    app = make_app(agent_project)
    ui = RecordingUI()
    gate = ActionGate(app.engine, ui, source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    session = ComputerSession(gate, actor=Actor.USER, state_dir=tmp_path / "state")
    executor = ActionExecutor(app, gate, actor=Actor.USER, computer=lambda: session)
    assert executor.run("android.emulators").output["avds"] == ["Pixel_6_API_33", "AndroidWorldAvd"]
    started = executor.run("android.emulator_start", {"avd": "AndroidWorldAvd", "timeout": 30})
    assert started.ok and started.output["serial"] == "emulator-5554" and ui.requests  # medium risk: asked
    assert executor.plan("android.emulator_start", {"avd": "x", "wipe": True}).decision.risk >= 3  # wiping data is high
    ui.action_answers = [False]
    assert executor.run("android.emulator_stop", {"device": "emulator-5554"}).status == "denied"
    assert sdk.calls[-1][1:] != ["-s", "emulator-5554", "emu", "kill"]  # declined: nothing was sent


# ------------------------------------------------------- AndroidWorld-style tasks
def test_registered_tasks_follow_the_androidworld_contract() -> None:
    device = FakeDevice()
    adb = AdbClient("adb", serial=device.serial, runner=device)
    task = task_class("open_app")({"package": SETTINGS})
    assert task.goal == f"Open the app {SETTINGS}" and task.complexity == 1.0
    task.initialize_task(adb)
    assert task.is_successful(adb) == 0.0
    adb.launch(SETTINGS)
    assert task.is_successful(adb) == 1.0
    with pytest.raises(KeyError, match="no Android task named"):
        task_class("solve_captcha")

    @register
    class Custom(AndroidTask):
        name = "custom_test_task"

        def is_successful(self, env: AdbClient) -> float:
            return 0.5

    assert task_class("custom_test_task") is Custom


def test_a_real_device_task_without_adb_is_skipped_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIGHHX_ADB", "/nonexistent/adb")
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(DeviceUnavailable, match="adb"):
        device_for({})
    from highhx.benchmarks.model import BenchmarkTask

    task = {
        "id": "open-settings",
        "environment": {"kind": "android_device", "task": "open_app", "params": {"package": SETTINGS}},
        "planner": {"kind": "scripted", "steps": [{"action": "launch", "parameters": {"name": SETTINGS}}]},
    }
    suite = BenchmarkSuite("android-world", "", [BenchmarkTask.from_dict(task)])
    result = BenchmarkRunner().run_suite(suite)
    assert result.runs == [] and result.skipped == [{"task": "open-settings", "reason": result.skipped[0]["reason"]}]
    assert "unavailable" in result.skipped[0]["reason"] and "adb" in result.skipped[0]["reason"]


def test_a_real_device_task_runs_and_is_scored_by_the_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a device present (simulated adb here), the agent loop acts through the executor and the
    task's own is_successful decides — not the agent."""
    from highhx.benchmarks.model import BenchmarkTask

    device = FakeDevice()
    monkeypatch.setenv("HIGHHX_ADB", sys.executable)  # an existing file stands in for adb
    monkeypatch.setattr(adb_module, "subprocess_runner", device)
    suite = BenchmarkSuite(
        "android-world",
        "",
        [
            BenchmarkTask.from_dict(
                {
                    "id": "open-settings",
                    "environment": {"kind": "android_device", "task": "open_app", "params": {"package": SETTINGS}},
                    "planner": {"kind": "scripted", "steps": [{"action": "launch", "parameters": {"name": SETTINGS}}]},
                    "risk_level": "high",
                }
            ),
            BenchmarkTask.from_dict(
                {
                    "id": "claims-but-fails",
                    "environment": {
                        "kind": "android_device",
                        "task": "screen_shows_text",
                        "params": {"text": "Never there"},
                    },
                    "planner": {"kind": "scripted", "steps": [{"action": "home"}]},
                    "risk_level": "high",
                }
            ),
        ],
    )
    result = BenchmarkRunner().run_suite(suite)
    by_id = {r.task_id: r for r in result.runs}
    assert by_id[
        "open-settings"
    ].success  # scored by OpenApp.is_successful on the device (the next task then went home)
    assert not by_id["claims-but-fails"].success  # the agent finished its script; the device says no
    assert any(call[-4:] == ["monkey", "-p", SETTINGS, "-c"] or SETTINGS in " ".join(call) for call in device.calls)
    assert result.environment["capabilities"]  # where it ran is recorded with the result


def test_device_health_is_read_not_guessed() -> None:
    device = FakeDevice()
    adb = AdbClient("adb", serial=device.serial, runner=device)
    report = adb.health()
    assert report == {
        "serial": device.serial,
        "booted": True,
        "android": "14",
        "model": "Pixel 8",
        "battery": 80,
        "screen_on": True,
        "free_storage": "3800000",
        "healthy": True,
        "problems": [],
    }
    device.battery, device.screen_on = 5, False
    sick = adb.health()
    assert not sick["healthy"] and sick["problems"] == ["battery at 5%", "screen is off"]


def test_emulator_reset_wipes_and_restarts(tmp_path: Path) -> None:
    sdk = FakeSDK()
    emu = emulator(sdk)
    emu.start("AndroidWorldAvd", log=tmp_path / "e.log", timeout=5)
    sdk.started.clear()
    sdk.boot_polls = 0
    original_devices = sdk.__call__

    def after_kill(argv: list[str], timeout: float, cancel: Any) -> tuple[int, bytes, bytes]:
        if argv[1:] == ["-s", "emulator-5554", "emu", "kill"]:
            sdk.started.clear()  # the emulator is gone until it is started again
        return original_devices(argv, timeout, cancel)

    emu.runner = after_kill
    emu.adb = AdbClient("adb", runner=after_kill)
    started = emu.reset("emulator-5554", "AndroidWorldAvd", log=tmp_path / "e.log", timeout=5)
    assert started["serial"] == "emulator-5554" and "-wipe-data" in sdk.started[-1]
