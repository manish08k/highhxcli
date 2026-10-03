"""The ComputerDriver interface: desktop (the existing HighhXDriver on a simulated desktop),
platform guards, the browser adapter, VM honesty and discovery."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from highhx.automation.engine.bridge import AutomationBridge
from highhx.computer.driver import HighhXDriver
from highhx.computer.providers import Capability
from highhx.drivers import OPERATIONS, ComputerDriver, discover
from highhx.drivers.android import AdbClient, AndroidDriver
from highhx.drivers.base import CapabilityError
from highhx.drivers.browser import BrowserDriver
from highhx.drivers.desktop import (
    DesktopDriver,
    LinuxDriver,
    MacDriver,
    RemoteDriver,
    WindowsDriver,
    local_driver_class,
)
from highhx.drivers.vm import VMDriver
from tests.computer_use.environment import SimulatedDesktop

DESKTOP = Path(__file__).resolve().parents[2] / "computer_use" / "tasks" / "_desktop.yaml"


@pytest.fixture
def desktop() -> tuple[DesktopDriver, SimulatedDesktop]:
    env = SimulatedDesktop(yaml.safe_load(DESKTOP.read_text()))
    return DesktopDriver(HighhXDriver(AutomationBridge(env))), env


def test_every_driver_implements_the_interface(desktop) -> None:
    driver, _ = desktop
    assert isinstance(driver, ComputerDriver)
    assert isinstance(AndroidDriver(AdbClient(adb="adb")), ComputerDriver)
    assert isinstance(BrowserDriver(FakeBrowser()), ComputerDriver)
    for op in OPERATIONS:
        assert callable(getattr(DesktopDriver, op)) and callable(getattr(AndroidDriver, op)) and callable(getattr(BrowserDriver, op))


def test_desktop_driver_adapts_the_existing_computer_api(desktop) -> None:
    driver, env = desktop
    state = driver.observe()
    assert state.surface == "desktop" and state.active_app == "Notes"
    increment = state.find(role="button", name="Increment")[0]
    driver.click(*increment.center)
    title = state.find(name="Title")[0]
    driver.click(*title.center)
    driver.type("Plan")
    assert env.element("Increment")["presses"] == 1 and env.element("Title")["value"] == "Plan"
    caps = driver.capabilities()
    assert caps.surface == "desktop" and set(OPERATIONS) <= set(caps.features)


def test_platform_drivers_refuse_other_platforms(desktop) -> None:
    driver, _ = desktop
    engine = driver.driver
    wrong = {"darwin": WindowsDriver, "win32": MacDriver}.get(sys.platform, MacDriver)
    with pytest.raises(CapabilityError):
        wrong(engine)
    assert local_driver_class()(engine).surface == "desktop"
    if sys.platform.startswith("linux"):
        assert local_driver_class() is LinuxDriver


def test_remote_driver_needs_an_ssh_target(desktop) -> None:
    driver, _ = desktop
    with pytest.raises(CapabilityError, match="ssh://"):
        RemoteDriver(driver.driver)


def test_vm_driver_is_honest() -> None:
    with pytest.raises(CapabilityError) as caught:
        VMDriver()
    assert "not supported" in caught.value.message and "sandbox" in (caught.value.hint or "")
    caps = VMDriver.capabilities()
    assert not any(caps.supports(op) for op in OPERATIONS)
    with pytest.raises(CapabilityError):
        caps.require("click")


def test_discovery_lists_every_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIGHHX_ADB", "/nonexistent/adb")
    found = {d["driver"]: d for d in discover()}
    assert set(found) == {"desktop", "browser", "android", "remote", "vm"}
    assert found["android"]["available"] is False and "adb" in str(found["android"]["detail"])
    assert found["vm"]["available"] is False


class FakeBrowser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def capability(self) -> Capability:
        return Capability("browser", True, "fake")

    def pointer(self, kind: str, x: float, y: float, **kw: Any) -> None:
        self.calls.append(("pointer", (kind, x, y, kw.get("count", 1), kw.get("to"), kw.get("direction"))))

    def insert_text(self, text: str, **kw: Any) -> None:
        self.calls.append(("insert_text", text))

    def key_combo(self, modifiers: list[str], key: str, **kw: Any) -> None:
        self.calls.append(("key_combo", (modifiers, key)))

    def press(self, key: str, **kw: Any) -> None:
        self.calls.append(("press", key))

    def scroll(self, direction: str, **kw: Any) -> None:
        self.calls.append(("scroll", direction))


def test_browser_driver_maps_operations_to_devtools_input() -> None:
    browser = FakeBrowser()
    driver = BrowserDriver(browser)  # type: ignore[arg-type]
    driver.click(10, 20)
    driver.double_click(10, 20)
    driver.type("hi")
    driver.hotkey("cmd+a")
    driver.scroll("down", at=(5, 5))
    driver.drag(1, 2, 3, 4)
    assert browser.calls == [
        ("pointer", ("click", 10, 20, 1, None, None)),
        ("pointer", ("click", 10, 20, 2, None, None)),
        ("insert_text", "hi"),
        ("key_combo", (["command"], "a")),
        ("pointer", ("wheel", 5, 5, 1, None, "down")),
        ("pointer", ("drag", 1, 2, 1, (3, 4), None)),
    ]
    with pytest.raises(CapabilityError):
        driver.hotkey("hyper+a")
    with pytest.raises(CapabilityError):
        driver.scroll("left")
