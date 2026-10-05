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

from tests.unit.agent.conftest import agent_project, make_app  # noqa: F401

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


def test_a_manual_drag_with_button_down_and_up(fixture_app: tuple[Any, Any]) -> None:
    driver, state = fixture_app
    x, y, width, height = _element(driver, "Volume").bounds
    high = state()["volume"] > 50  # drag toward the other end, wherever the slider is now
    start, end = (x + width - 6, x + 6) if high else (x + 6, x + width - 6)
    driver.mouse_down(start, y + height // 2)
    try:
        for step in range(1, 9):
            driver.move(start + (end - start) * step // 8, y + height // 2)
            time.sleep(0.03)
    finally:
        driver.mouse_up(end, y + height // 2)
    moved = state(lambda s: (s["volume"] < 50) if high else (s["volume"] > 50))
    assert (moved["volume"] < 50) if high else (moved["volume"] > 50)


def test_one_exact_window_of_an_application_comes_to_the_front(fixture_app: tuple[Any, Any]) -> None:
    driver, _state = fixture_app
    main, other = sorted(driver.windows(APP), key=lambda w: w.width, reverse=True)[:2]
    try:
        assert driver.focus_window(other.id)["frontmost"] is True
        assert driver.windows()[0].id == other.id
    finally:
        assert driver.focus_window(main.id)["frontmost"] is True  # back as the other tests expect
    assert driver.windows()[0].id == main.id


def test_a_region_screenshot(fixture_app: tuple[Any, Any]) -> None:
    from highhx.automation.engine.bridge import EngineError
    from highhx.automation.engine.platforms import quartz

    driver, _state = fixture_app
    window = driver.windows(APP)[0]
    region = (window.x, window.y, 200, 100)
    if not quartz.screen_capture_allowed():  # without the grant, a refusal — never a blank image
        with pytest.raises(EngineError) as info:
            driver.screenshot(region=region)
        assert info.value.code == "screen_recording_denied"
        return
    shot = driver.screenshot(region=region)
    try:
        assert (shot.width, shot.height) == (round(200 * shot.scale), round(100 * shot.scale))
    finally:
        shot.path.unlink(missing_ok=True)


# ---------------------------------------- through the executor (the path every caller uses)
@pytest.fixture
def act(fixture_app: tuple[Any, Any], make_app: Any, agent_project: Path) -> Iterator[Callable[..., Any]]:  # noqa: F811
    from highhx.actions.executor import ActionExecutor
    from highhx.computer.session import ComputerSession
    from highhx.safety.actions import Actor
    from highhx.safety.audit import AuditLog
    from highhx.safety.gate import ActionGate, ApprovalMode
    from tests.unit.agent.conftest import RecordingUI

    app = make_app(agent_project)
    gate = ActionGate(
        app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor)
    )
    session = ComputerSession(gate, actor=Actor.USER)
    executor = ActionExecutor(app, gate, actor=Actor.USER, computer=lambda: session)
    fixture_app[0].focus(APP)

    def run(name: str, inputs: dict[str, Any]) -> Any:
        result = executor.run(name, inputs)
        assert result.ok, (name, result.error)
        return result

    yield run
    executor.close()
    session.close()


def test_every_mouse_button_and_horizontal_scroll_reach_the_application(fixture_app: tuple[Any, Any], act: Any) -> None:
    driver, state = fixture_app
    # The pad is a plain view (not in the accessibility tree): it sits just left of "Tiny" in one
    # row (NSStackView's default spacing is 8pt; the pad is 200pt wide).
    tx, ty, _tw, th = _element(driver, "Tiny").bounds
    x, y = tx - 8 - 100, ty + th // 2
    before = state()["pad"]
    for button in ("left", "right", "middle"):
        act("computer.click_at", {"x": x, "y": y, "button": button})
        state(lambda s, b=button: s["pad"].get(b, 0) == before.get(b, 0) + 1)
    for direction in ("left", "right", "down"):
        act("computer.scroll", {"source": "desktop", "direction": direction, "x": x, "y": y, "amount": 3})
        state(lambda s, d=direction: s["pad"].get(f"scroll-{d}", 0) > before.get(f"scroll-{d}", 0))


def test_small_controls_are_hit_precisely_at_their_edges(fixture_app: tuple[Any, Any], act: Any) -> None:
    driver, state = fixture_app
    x, y, width, height = _element(driver, "Tiny").bounds
    assert width <= 12 and height <= 12, (width, height)
    count = state()["tiny"]
    for px, py in ((x + width // 2, y + height // 2), (x + 1, y + 1), (x + width - 2, y + height - 2)):
        act("computer.click_at", {"x": px, "y": py})
        count += 1
        state(lambda s, c=count: s["tiny"] == c)
    act("computer.click_at", {"x": x + width + 6, "y": y + height // 2})  # just outside: must not count
    time.sleep(0.6)
    assert state()["tiny"] == count


def test_copy_cut_paste_and_editing_keys_in_a_field(fixture_app: tuple[Any, Any], act: Any) -> None:
    driver, state = fixture_app
    original = driver.clipboard_read()
    try:
        act("computer.clipboard_write", {"text": "from-clipboard"})
        x, y = _center(_element(driver, "Name"))
        act("computer.click_at", {"x": x, "y": y})
        act("computer.edit", {"op": "select_all"})
        act("computer.edit", {"op": "paste"})
        state(lambda s: s["name"] == "from-clipboard")
        act("computer.press", {"key": "backspace"})
        state(lambda s: s["name"] == "from-clipboar")
        act("computer.edit", {"op": "select_all"})
        act("computer.edit", {"op": "copy"})
        assert driver.clipboard_read() == "from-clipboar"
        act("computer.edit", {"op": "cut"})
        state(lambda s: s["name"] == "")
        act("computer.edit", {"op": "undo"})
        state(lambda s: s["name"] == "from-clipboar")
        act("computer.hotkey", {"keys": "cmd+a"})
        act("computer.type", {"text": "Z9!@ é"})
        state(lambda s: s["name"] == "Z9!@ é")
    finally:
        driver.clipboard_write(original or " ")


def test_maximize_and_minimize_a_real_window(fixture_app: tuple[Any, Any], act: Any) -> None:
    driver, state = fixture_app
    screen = driver.screen()
    act("computer.window_state", {"state": "maximize", "app": APP})
    assert state(lambda s: s["frame"][2] >= 0.9 * screen.width)["frame"][2] >= 0.9 * screen.width
    act("computer.window_state", {"state": "minimize", "app": APP})
    assert state(lambda s: s["minimized"])["minimized"] is True
    subprocess.run(  # put it back for any test that runs after this one (test-side restore)
        [
            "osascript",
            "-e",
            f'tell application "System Events" to set value of attribute "AXMinimized" of window "HighhX Fixture" of process "{APP}" to false',
        ],
        check=False,
        capture_output=True,
    )
    state(lambda s: not s["minimized"], timeout=8)
    driver.focus(APP)


def test_double_click_is_one_double_click_not_two_singles(fixture_app: tuple[Any, Any], act: Any) -> None:
    driver, state = fixture_app
    tx, ty, _tw, th = _element(driver, "Tiny").bounds
    before = state()["pad"]
    time.sleep(0.6)  # past the double-click interval of any earlier click
    act("computer.click_at", {"x": tx - 108, "y": ty + th // 2, "count": 2})
    after = state(lambda s: s["pad"].get("double", 0) == before.get("double", 0) + 1)["pad"]
    assert after.get("left", 0) == before.get("left", 0) + 1  # the first press of the pair, then the double


def test_navigation_keys_inside_a_field(fixture_app: tuple[Any, Any], act: Any) -> None:
    # macOS semantics: cmd+left/right move the caret to the line's ends (Home/End scroll, below).
    driver, state = fixture_app
    act("computer.click_at", dict(zip(("x", "y"), _center(_element(driver, "Name")), strict=True)))
    act("computer.hotkey", {"keys": "cmd+a"})
    act("computer.type", {"text": "abc"})
    state(lambda s: s["name"] == "abc")
    steps = (("cmd+left", "X", "Xabc"), ("cmd+right", "Y", "XabcY"), ("left", "-", "Xabc-Y"), ("right", "+", "Xabc-Y+"))
    for keys, text, expected in steps:
        act("computer.hotkey", {"keys": keys})
        act("computer.type", {"text": text})
        state(lambda s, e=expected: s["name"] == e)
    act("computer.hotkey", {"keys": "cmd+left"})
    act("computer.press", {"key": "forwarddelete"})  # removes the "X" after the caret
    state(lambda s: s["name"] == "abc-Y+")
    act("computer.hotkey", {"keys": "cmd+right"})
    act("computer.press", {"key": "backspace"})  # removes the "+" before the caret
    state(lambda s: s["name"] == "abc-Y")
    act("computer.hotkey", {"keys": "shift+left"})  # a selection, replaced by what is typed
    act("computer.type", {"text": "Z"})
    state(lambda s: s["name"] == "abc-Z")
    act("computer.press", {"key": "escape"})  # Escape in a field never edits it
    time.sleep(0.4)
    assert state()["name"] == "abc-Z"


def test_page_and_document_keys_scroll_a_text_view(fixture_app: tuple[Any, Any], act: Any) -> None:
    driver, state = fixture_app
    x, y, _width, height = _element(driver, "Volume").bounds
    act("computer.click_at", {"x": x + 60, "y": y + height + 40})  # inside the "Lines" text view
    act("computer.press", {"key": "home"})
    state(lambda s: s["scrolled"] == 0)
    act("computer.press", {"key": "pagedown"})
    first = state(lambda s: s["scrolled"] > 0)["scrolled"]
    act("computer.press", {"key": "pagedown"})
    state(lambda s: s["scrolled"] > first)
    act("computer.press", {"key": "pageup"})
    state(lambda s: 0 < s["scrolled"] < first + 5)
    act("computer.press", {"key": "end"})
    state(lambda s: s["scrolled"] > 1000)  # 200 lines: the end is far down
    act("computer.press", {"key": "home"})
    state(lambda s: s["scrolled"] == 0)


def test_tab_and_shift_tab_move_focus(fixture_app: tuple[Any, Any], act: Any) -> None:
    driver, _state = fixture_app

    def focus_becomes(wanted: str) -> str:
        current = ""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            hit = next((e for e in driver.get_ui_tree(APP).elements if e.focused), None)
            current = f"{hit.role}:{hit.name}" if hit is not None else ""
            if current == wanted:
                break
            time.sleep(0.1)
        return current

    act("computer.click_at", dict(zip(("x", "y"), _center(_element(driver, "Name")), strict=True)))
    assert focus_becomes("textbox:Name") == "textbox:Name"
    act("computer.press", {"key": "tab"})
    assert focus_becomes("textbox:Email") == "textbox:Email"
    act("computer.hotkey", {"keys": "shift+tab"})
    assert focus_becomes("textbox:Name") == "textbox:Name"
    act("computer.press", {"key": "tab"})
    act("computer.press", {"key": "tab"})  # into the multi-line text view (where Tab is a character)
    assert focus_becomes("textbox:text entry area") == "textbox:text entry area"
    act("computer.hotkey", {"keys": "ctrl+shift+tab"})  # the macOS way out of a text view
    assert focus_becomes("textbox:Email") == "textbox:Email"


def test_clicks_land_on_the_control_after_the_window_moves_and_resizes(fixture_app: tuple[Any, Any], act: Any) -> None:
    driver, state = fixture_app
    for frame in ((300, 160, 560, 520), (60, 90, 500, 500)):
        window = driver.windows(APP)[0]
        driver.set_window_frame(window.id, *frame)
        state(lambda s, f=frame: s["frame"][2] == f[2])
        before = state()["count"]
        act("computer.click_at", dict(zip(("x", "y"), _center(_element(driver, "Increment")), strict=True)))
        state(lambda s, b=before: s["count"] == b + 1)
