"""The HighhX Computer Runtime on a real macOS desktop, against HighhX's own AppKit fixture
(tests/fixtures/desktop/HighhXFixture.swift), with real pointer and keyboard input.

It takes over the mouse and keyboard for about half a minute, so it only runs when asked:

    HIGHHX_TEST_DESKTOP_INPUT=1 pytest tests/e2e/test_desktop_live.py

and needs macOS, swiftc and Accessibility permission for the terminal (it skips, saying why,
otherwise). Every check reads the fixture's own state file: a click counts only if the
application saw it.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform != "darwin", reason="the live desktop fixture is an AppKit application"),
    pytest.mark.skipif(
        os.environ.get("HIGHHX_TEST_DESKTOP_INPUT") != "1",
        reason="set HIGHHX_TEST_DESKTOP_INPUT=1 to let the test drive the real mouse and keyboard",
    ),
]

SOURCE = Path(__file__).resolve().parents[1] / "fixtures" / "desktop" / "HighhXFixture.swift"
APP = "HighhXFixture"


def _bundle(folder: Path) -> Path:
    """HighhXFixture.app — built from source (a real bundle, so LaunchServices knows its name)."""
    bundle = folder / f"{APP}.app"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    subprocess.run(["swiftc", "-O", "-o", str(macos / APP), str(SOURCE)], check=True, capture_output=True)
    info = {
        "CFBundleName": APP,
        "CFBundleIdentifier": "dev.highhx.fixture",
        "CFBundleExecutable": APP,
        "CFBundlePackageType": "APPL",
        "NSPrincipalClass": "NSApplication",
        "LSMinimumSystemVersion": "12.0",
    }
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    return bundle


@pytest.fixture(scope="module")
def fixture_app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Any, Callable[..., dict[str, Any]]]]:
    from highhx.automation.engine.platforms import quartz
    from highhx.computer import HighhXDriver

    if shutil.which("swiftc") is None:
        pytest.skip("swiftc is not installed")
    if not quartz.accessibility_trusted():
        pytest.skip("grant Accessibility to this terminal (System Settings → Privacy & Security → Accessibility)")
    folder = tmp_path_factory.mktemp("desktop")
    state_file = folder / "state.json"
    bundle = _bundle(folder)
    subprocess.run(["open", "-n", str(bundle), "--args", str(state_file)], check=True)

    def state(until: Callable[[dict[str, Any]], bool] = lambda _s: True, timeout: float = 6.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        current: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                current = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = {}
            if current and until(current):
                return current
            time.sleep(0.1)
        raise AssertionError(f"the fixture never reached the expected state (last: {current})")

    state()
    driver = HighhXDriver.create()
    driver.focus(APP)
    try:
        yield driver, state
    finally:
        try:
            driver.quit(APP)
        finally:
            driver.end_session()


def _element(driver: Any, name: str) -> Any:
    tree = driver.get_ui_tree(APP)
    found = next((e for e in tree.elements if e.name == name and e.bounds), None)
    assert found is not None, f"{name!r} is not in the accessibility tree: {[(e.role, e.name) for e in tree.elements]}"
    return found


def _center(element: Any) -> tuple[int, int]:
    x, y, width, height = element.bounds
    return x + width // 2, y + height // 2


def test_observation_of_a_real_application(fixture_app: tuple[Any, Any]) -> None:
    driver, _state = fixture_app
    front, _title, _window = driver.active()
    assert front == APP
    windows = driver.windows(APP)
    assert windows and windows[0].width >= 400
    roles = {(e.role, e.name) for e in driver.get_ui_tree(APP).elements}
    assert {("button", "Increment"), ("textbox", "Name"), ("checkbox", "Agree"), ("slider", "Volume")} <= roles
    at = driver.element_at(*_center(_element(driver, "Increment")))
    assert at.name == "Increment" and at.attributes["app"] == APP


def test_real_pointer_and_semantic_presses(fixture_app: tuple[Any, Any]) -> None:
    driver, state = fixture_app
    before = state()["count"]
    driver.click(*_center(_element(driver, "Increment")))  # a real click at a point
    state(lambda s: s["count"] == before + 1)
    driver.press_element("Increment", role="button", app=APP)  # an accessibility press, no coordinates
    state(lambda s: s["count"] == before + 2)
    driver.click(*_center(_element(driver, "Agree")))
    assert state(lambda s: s["agree"])["agree"] is True


def test_real_keyboard_into_a_focused_field(fixture_app: tuple[Any, Any]) -> None:
    driver, state = fixture_app
    driver.click(*_center(_element(driver, "Name")))
    driver.type_text("hello")
    assert state(lambda s: s["name"] == "hello")["name"] == "hello"


def test_drag_scroll_and_menus(fixture_app: tuple[Any, Any]) -> None:
    driver, state = fixture_app
    x, y, width, height = _element(driver, "Volume").bounds
    driver.drag((x + 6, y + height // 2), (x + width - 6, y + height // 2), duration_ms=400)
    assert state(lambda s: s["volume"] > 50)["volume"] > 50
    driver.scroll("down", 5, at=(x + 100, y + height + 70))
    assert state(lambda s: s["scrolled"] > 0)["scrolled"] > 0
    driver.invoke_menu(APP, "Fixture > Reset")
    assert state(lambda s: s["count"] == 0)["count"] == 0


def test_window_geometry_and_the_clipboard(fixture_app: tuple[Any, Any]) -> None:
    driver, state = fixture_app
    window = driver.windows(APP)[0]
    result = driver.set_window_frame(window.id, 120, 120, 620, 420)
    assert result["frame"][2:] == [620, 420]
    assert state(lambda s: s["frame"][2] == 620)["frame"][2] == 620
    original = driver.clipboard_read()
    try:
        driver.clipboard_write("highhx-e2e")
        assert driver.clipboard_read() == "highhx-e2e"
    finally:
        driver.clipboard_write(original or " ")


def test_same_named_controls_are_targeted_exactly(fixture_app: tuple[Any, Any]) -> None:
    from highhx.automation.engine.bridge import EngineError
    from highhx.automation.engine.provider import BridgeDesktopProvider

    driver, state = fixture_app
    before = state()["adds"]
    provider = BridgeDesktopProvider(driver, APP)
    adds = [e for e in provider.observe().elements if e.name == "Add"]
    assert len(adds) == 2 and adds[0].bounds != adds[1].bounds
    provider.click(adds[1].id)  # the second "Add", as observed
    assert state(lambda s: s["adds"] == [before[0], before[1] + 1])["adds"] == [before[0], before[1] + 1]
    for kwargs, code in (({}, "ambiguous_target"), ({"index": 0, "bounds": (1, 1, 5, 5)}, "stale_target")):
        with pytest.raises(EngineError) as info:
            driver.press_element("Add", role="button", app=APP, **kwargs)
        assert info.value.code == code
    time.sleep(0.5)
    assert state()["adds"] == [before[0], before[1] + 1]  # the refusals pressed nothing


def test_verify_state_against_a_real_window(fixture_app: tuple[Any, Any]) -> None:
    driver, _state = fixture_app
    window = driver.windows(APP)[0]
    frame = {"x": window.x, "y": window.y, "width": window.width, "height": window.height}
    check = driver.verify_state(
        window.id,
        [
            {"window": {"bounds": frame}},
            {"element": {"selector": {"role": "button", "label_contains": "Increment"}, "enabled": True}},
        ],
    )
    assert check.ok, check.to_dict()
    moved = driver.verify_state(window.id, [{"window": {"bounds": {**frame, "x": window.x + 50}}}], timeout_ms=0)
    assert moved.status == "unsatisfied"
