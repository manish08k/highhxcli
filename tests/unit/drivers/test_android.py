"""Android: the adb client (argv, quoting, parsing), the hierarchy, the driver, and android.*
actions through the executor — risk, approvals, grounding by text, verification."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from highhx.actions.executor import ActionExecutor
from highhx.actions.handlers.state import state_from_result
from highhx.actions.policy import Risk
from highhx.core.errors import UsageError
from highhx.drivers.android import AdbClient, AdbError, AndroidDriver, parse_devices, parse_hierarchy
from highhx.drivers.android.adb import escape_text
from highhx.drivers.base import CapabilityError
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.unit.agent.conftest import RecordingUI
from tests.unit.drivers.fake_adb import NOTES, SETTINGS, FakeDevice


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch) -> FakeDevice:
    fake = FakeDevice()
    monkeypatch.setenv("HIGHHX_ADB", sys.executable)  # "found"; the runner below answers instead
    monkeypatch.setattr("highhx.drivers.android.adb.subprocess_runner", fake)
    return fake


def _executor(make_app, root: Path, ui: RecordingUI | None = None, actor: Actor = Actor.USER) -> tuple[ActionExecutor, RecordingUI]:
    app = make_app(root)
    ui = ui or RecordingUI()
    gate = ActionGate(app.engine, ui, source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    return ActionExecutor(app, gate, actor=actor, sleep=lambda _s: None), ui


# ------------------------------------------------------------------ client
def test_devices_are_parsed() -> None:
    text = (
        "List of devices attached\n"
        "emulator-5554\tdevice product:sdk model:Pixel_8 transport_id:1\n"
        "R58M\tunauthorized usb:1-1 transport_id:2\n\n"
    )
    first, second = parse_devices(text)
    assert first.serial == "emulator-5554" and first.ready and first.emulator and first.model == "Pixel_8"
    assert second.state == "unauthorized" and not second.ready


def test_typed_text_cannot_become_a_device_command(device: FakeDevice) -> None:
    adb = AdbClient(serial=device.serial)
    adb.text("hello world; rm -rf /sdcard")
    assert device.typed == ["hello world; rm -rf /sdcard"]  # one literal string reached `input text`
    assert escape_text("50% off") == r"'50\%%soff'"
    with pytest.raises(UsageError):
        escape_text("two\nlines")


def test_argument_validation(device: FakeDevice) -> None:
    with pytest.raises(UsageError):
        AdbClient(serial="emu; reboot")
    adb = AdbClient(serial=device.serial)
    for bad in ("com.example;reboot", "notapackage", "../x"):
        with pytest.raises(UsageError):
            adb.launch(bad)
    with pytest.raises(UsageError):
        adb.keyevent("explode")
    adb.keyevent("KEYCODE_VOLUME_MUTE")
    assert device.calls[-1][-3:] == ["input", "keyevent", "KEYCODE_VOLUME_MUTE"]


def test_device_selection(device: FakeDevice) -> None:
    adb = AdbClient()
    assert adb.require_device().serial == "emulator-5554" and adb.serial == "emulator-5554"
    device.extra_devices = ["192.168.1.9:5555"]
    with pytest.raises(AdbError, match="2 devices"):
        AdbClient().require_device()
    device.state = "unauthorized"
    with pytest.raises(AdbError) as caught:
        AdbClient(serial="emulator-5554").require_device()
    assert "USB debugging" in (caught.value.hint or "")


def test_missing_adb_is_a_capability_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIGHHX_ADB", "/nonexistent/adb")
    adb = AdbClient()
    assert not adb.capability().available and "platform tools" in adb.capability().detail
    with pytest.raises(AdbError):
        adb.devices()
    driver = AndroidDriver(adb)
    assert not driver.capabilities().supports("click")


# --------------------------------------------------------------- hierarchy
def test_hierarchy_roles_names_and_secrets() -> None:
    fake = FakeDevice(focused_app=NOTES, field_value="Groceries")
    elements, text = parse_hierarchy(fake.hierarchy().split("UI hierchary")[0])
    by_name = {e.name: e for e in elements}
    assert by_name["Save"].role == "button" and by_name["Save"].bounds == (40, 420, 460, 100)
    assert by_name["Save"].attr("resource_id") == f"{NOTES}:id/save"
    title = by_name["Title"]
    assert title.role == "textbox" and title.value == "Groceries"
    assert by_name["Pin"].role == "switch" and by_name["Pin"].checked is False
    password = by_name["Password"]
    assert password.secret and password.value == ""
    assert "Notes" in text and all(e.sources == ("android",) for e in elements)


# ------------------------------------------------------------------ driver
def test_driver_capabilities_are_honest(device: FakeDevice) -> None:
    driver = AndroidDriver(AdbClient(serial=device.serial))
    caps = driver.capabilities()
    assert caps.supports("click") and caps.supports("launch") and not caps.supports("move") and not caps.supports("hotkey")
    with pytest.raises(CapabilityError):
        driver.move(1, 2)
    with pytest.raises(CapabilityError):
        driver.hotkey("ctrl c")


def test_driver_observes_a_full_state(device: FakeDevice) -> None:
    device.focused_app = NOTES
    state = AndroidDriver(AdbClient(serial=device.serial)).observe(screenshot=True)
    assert state.surface == "android" and state.active_app == NOTES and state.device.screen == (1080, 1920)
    assert state.device.serial == "emulator-5554" and state.screenshot.width == 108
    assert state.find(role="button", name="Save")


def test_driver_scroll_and_drag_are_swipes(device: FakeDevice) -> None:
    driver = AndroidDriver(AdbClient(serial=device.serial))
    driver.scroll("down")
    x1, y1, x2, y2 = (int(v) for v in device.calls[-1][-5:-1])
    assert x1 == x2 == 540 and y1 > y2  # the finger moves up to scroll down
    with pytest.raises(CapabilityError):
        driver.scroll("sideways")


# ----------------------------------------------------------------- actions
def test_android_actions_go_through_the_executor(device: FakeDevice, agent_project: Path, make_app) -> None:
    executor, ui = _executor(make_app, agent_project)
    listed = executor.run("android.devices")
    assert listed.ok and listed.output["devices"][0]["serial"] == "emulator-5554"
    launched = executor.run("android.launch", {"package": NOTES})
    assert launched.ok and launched.verified and device.focused_app == NOTES
    missing = executor.run("android.launch", {"package": "com.example.missing"})
    assert not missing.ok and "Could not launch" in missing.error
    observed = executor.run("android.observe", {})
    state = state_from_result(observed)
    assert observed.ok and state.surface == "android" and state.find(name="Save")
    assert ui.requests == []  # reads and a launch are not asked for the user's own actions


def test_tap_by_text_is_grounded_on_the_hierarchy(device: FakeDevice, agent_project: Path, make_app) -> None:
    device.focused_app = NOTES
    executor, _ = _executor(make_app, agent_project)
    tapped = executor.run("android.tap", {"text": "Save"})
    assert tapped.ok and device.taps == [(270, 470)] and tapped.output["grounded"]["candidate"]["strategy"] == "accessibility"
    device.duplicate_delete = True
    ambiguous = executor.run("android.tap", {"text": "Delete", "role": "button"}, )
    assert not ambiguous.ok and "several" in ambiguous.error and len(device.taps) == 1


def test_typing_is_verified_by_the_field(device: FakeDevice, agent_project: Path, make_app) -> None:
    device.focused_app = NOTES
    executor, _ = _executor(make_app, agent_project)
    executor.run("android.tap", {"x": 500, "y": 250})
    typed = executor.run("android.type", {"text": "Buy milk"})
    assert typed.ok and typed.verified and device.field_value == "Buy milk"


def test_risk_follows_what_a_tap_or_app_change_can_do(device: FakeDevice, agent_project: Path, make_app) -> None:
    executor, ui = _executor(make_app, agent_project)
    assert executor.plan("android.tap", {"x": 1, "y": 2}).decision.risk == Risk.MEDIUM
    assert executor.plan("android.tap", {"text": "Delete account"}).decision.risk >= Risk.HIGH
    assert executor.plan("android.tap", {"text": "Settings"}).decision.risk == Risk.LOW
    assert executor.plan("android.key", {"key": "power"}).decision.risk == Risk.HIGH
    install = executor.plan("android.install", {"apk": "app.apk"})
    assert install.decision.risk == Risk.HIGH and install.spec.policy_name(install.inputs) == "android:install"
    ui.action_answers = [False]
    declined = executor.run("android.uninstall", {"package": SETTINGS})
    assert declined.status == "denied" and SETTINGS in device.packages  # nothing was removed
    removed = executor.run("android.uninstall", {"package": NOTES})
    assert removed.ok and NOTES not in device.packages


def test_policy_can_forbid_installing_apps(device: FakeDevice, agent_project: Path, make_app) -> None:
    (agent_project / ".highhx" / "policies.yaml").write_text(
        "rules:\n  - id: no-installs\n    effect: deny\n    when: {action: 'android:install'}\n"
    )
    (agent_project / "app.apk").write_bytes(b"PK")
    executor, _ = _executor(make_app, agent_project)
    assert executor.run("android.install", {"apk": "app.apk"}).status == "blocked"
    assert "com.example.installed" not in device.packages


def test_the_agent_is_asked_for_android_input(device: FakeDevice, agent_project: Path, make_app) -> None:
    executor, ui = _executor(make_app, agent_project, actor=Actor.AGENT)
    executor.run("android.back")
    assert ui.of("permission")


def test_no_device_is_a_clear_failure(device: FakeDevice, agent_project: Path, make_app) -> None:
    device.state = "offline"
    executor, _ = _executor(make_app, agent_project)
    result = executor.run("android.home")
    assert not result.ok and "No Android device" in result.error
