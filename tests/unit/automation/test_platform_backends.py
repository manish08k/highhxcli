"""The built-in engine's platform backends, each against a fake of its OS layer: what they send,
what they refuse, and that they never report an action that did not happen. macOS additionally
has read-only calls into the real frameworks (no permission needed)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from highhx.automation.engine.bridge import EngineError
from highhx.automation.engine.platforms import backend_for, quartz
from highhx.automation.engine.platforms import linux as linux_module
from highhx.automation.engine.platforms.linux import LinuxBackend
from highhx.automation.engine.platforms.macos import ANSI_KEYS, MacBackend
from highhx.automation.engine.platforms.windows import (
    KEYUP,
    UNICODE,
    VK,
    WM_CHAR,
    WindowsBackend,
    key_inputs,
    text_inputs,
)
from highhx.automation.engine.protocol import FEATURES, KEY_CODES
from tests.unit.automation.fakes import PNG_1X1


class Runner:
    """Records every argv; answers by the first matching marker (else success with ``default``)."""

    def __init__(self, replies: dict[str, tuple[int, str, str]] | None = None, default: str = "{}") -> None:
        self.argv: list[list[str]] = []
        self.replies = replies or {}
        self.default = default

    def __call__(self, argv: list[str], what: str) -> tuple[int, str, str]:
        self.argv.append(argv)
        joined = " ".join(argv)
        for marker, reply in self.replies.items():
            if marker in joined:
                return reply
        if argv[0] in ("screencapture", "import", "grim", "scrot"):
            Path(argv[-1]).write_bytes(PNG_1X1)
        return 0, self.default, ""


# ------------------------------------------------------------------- macOS
@pytest.fixture
def fake_quartz(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"trusted": True, "capture": True, "calls": []}

    def record(name: str) -> Any:
        return lambda *a, **k: state["calls"].append((name, a, k))

    monkeypatch.setattr(quartz, "accessibility_trusted", lambda: state["trusted"])
    monkeypatch.setattr(quartz, "screen_capture_allowed", lambda: state["capture"])
    for name in ("click", "move", "drag", "scroll", "key", "type_text"):
        monkeypatch.setattr(quartz, name, record(name))
    monkeypatch.setattr(
        quartz, "element_at", lambda x, y: {"pid": 70, "role": "button", "name": "Save", "bounds": [x, y, 1, 1]}
    )
    monkeypatch.setattr(quartz, "screen", lambda: {"width": 1470, "height": 956, "x": 0, "y": 0, "scale": 2.0})
    monkeypatch.setattr(
        quartz,
        "windows",
        lambda: [
            {
                "id": 5,
                "pid": 70,
                "app": "Notes",
                "title": "",
                "x": 0,
                "y": 25,
                "width": 800,
                "height": 600,
                "on_screen": True,
            }
        ],
    )
    return state


def test_mac_input_is_refused_not_dropped_without_accessibility(fake_quartz: dict[str, Any]) -> None:
    fake_quartz["trusted"] = False
    backend = MacBackend(Runner())
    for op, args in (("click_at", {"x": 1, "y": 2}), ("move", {"x": 1, "y": 2}), ("scroll", {"direction": "down"})):
        with pytest.raises(EngineError) as caught:
            backend.call(op, args)
        assert caught.value.code == "accessibility_denied"
    assert fake_quartz["calls"] == []  # macOS would have dropped the events silently: nothing was sent
    features = backend.op_capabilities()["features"]
    assert not features["pointer"]["available"] and "Accessibility" in features["pointer"]["detail"]


def test_mac_click_reports_what_was_under_the_pointer_and_delivers_in_the_background(
    fake_quartz: dict[str, Any],
) -> None:
    runner = Runner(default='{"pid": 42}')
    result = MacBackend(runner).call("click_at", {"x": 10, "y": 20, "button": "right", "count": 2, "app": "Notes"})
    assert result["element"]["name"] == "Save" and result["background"]
    assert fake_quartz["calls"] == [("click", (10, 20), {"button": "right", "count": 2, "pid": 42})]
    assert runner.argv[0][-1] == "Notes"  # the application name travels as an argument to a fixed script


def test_mac_background_keys_use_the_ansi_layout(fake_quartz: dict[str, Any]) -> None:
    backend = MacBackend(Runner(default='{"pid": 42}'))
    backend.call("hotkey", {"modifiers": ["command"], "key": "s", "app": "Notes"})
    backend.call("key", {"key": "enter", "app": "Notes"})
    assert [c[1][0] for c in fake_quartz["calls"]] == [ANSI_KEYS["s"], KEY_CODES["enter"]]
    with pytest.raises(EngineError) as caught:
        backend.call("key", {"key": "é", "app": "Notes"})
    assert caught.value.code == "unsupported"


def test_mac_scroll_is_real_wheel_input(fake_quartz: dict[str, Any]) -> None:
    MacBackend(Runner()).call("scroll", {"direction": "up", "amount": 2, "x": 5, "y": 6})
    assert fake_quartz["calls"] == [("scroll", (-6, 0), {"at": (5, 6)})]


def test_mac_screenshot_needs_screen_recording(fake_quartz: dict[str, Any], tmp_path: Path) -> None:
    fake_quartz["capture"] = False
    runner = Runner()
    with pytest.raises(EngineError) as caught:
        MacBackend(runner).call("screenshot", {"path": str(tmp_path / "a.png")})
    assert caught.value.code == "screen_recording_denied" and "Screen Recording" in (caught.value.hint or "")
    assert runner.argv == []
    fake_quartz["capture"] = True
    shot = MacBackend(runner).call("screenshot", {"path": str(tmp_path / "a.png"), "window": 5})
    assert runner.argv[-1] == ["screencapture", "-x", "-t", "png", "-o", "-l", "5", str(tmp_path / "a.png")]
    # the image maps to the window's frame: point = origin + pixel / scale (a 1-pixel fake of an 800-point window)
    assert (shot["width"], shot["height"], shot["origin"], shot["scale"]) == (1, 1, [0, 25], 1 / 800)


def test_mac_menu_errors_name_what_exists(fake_quartz: dict[str, Any]) -> None:
    missing = Runner(default=json.dumps({"error": "not_found", "level": 1, "names": ["Save", "Close"]}))
    with pytest.raises(EngineError, match=r"no menu item 'Export'.*Save, Close"):
        MacBackend(missing).call("menu", {"app": "Notes", "path": ["File", "Export"]})
    disabled = Runner(default=json.dumps({"error": "disabled"}))
    with pytest.raises(EngineError) as caught:
        MacBackend(disabled).call("menu", {"app": "Notes", "path": ["File", "Save"]})
    assert caught.value.code == "refused"


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS frameworks")
def test_the_real_quartz_bindings_answer_read_only_questions() -> None:
    screen = quartz.screen()
    assert screen["width"] > 0 and screen["height"] > 0 and screen["scale"] >= 1
    x, y = quartz.cursor()
    assert isinstance(x, int) and isinstance(y, int)
    assert all({"id", "pid", "app", "x", "y", "width", "height"} <= set(w) for w in quartz.windows())
    assert isinstance(quartz.accessibility_trusted(), bool) and isinstance(quartz.screen_capture_allowed(), bool)


# ------------------------------------------------------------------- Linux
@pytest.fixture
def x11(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    tools = {"xdotool", "xclip", "import", "wmctrl"}
    monkeypatch.setattr(linux_module.shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None)


def test_linux_input_goes_through_xdotool_with_text_as_an_argument(x11: None) -> None:
    runner = Runner(default="")
    backend = LinuxBackend(runner)
    backend.call("type", {"text": "--delay 0; rm -rf ~"})
    backend.call("hotkey", {"modifiers": ["command", "shift"], "key": "t"})
    backend.call("key", {"key": "pagedown"})
    backend.call("click_at", {"x": 10, "y": 20, "button": "right", "count": 2})
    assert runner.argv[0] == [
        "/usr/bin/xdotool",
        "type",
        "--clearmodifiers",
        "--delay",
        "8",
        "--",
        "--delay 0; rm -rf ~",
    ]
    assert runner.argv[1][-1] == "super+shift+t" and runner.argv[2][-1] == "Next"
    assert runner.argv[3:] == [
        ["/usr/bin/xdotool", "mousemove", "--sync", "10", "20"],
        ["/usr/bin/xdotool", "click", "--repeat", "2", "3"],
    ]


def test_linux_refuses_what_x11_cannot_do(x11: None) -> None:
    with pytest.raises(EngineError, match="background window"):
        LinuxBackend(Runner()).call("click_at", {"x": 1, "y": 2, "app": "gedit"})


def test_linux_wayland_and_missing_tools_are_said_not_faked(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = Runner()
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    with pytest.raises(EngineError, match="RemoteDesktop portal") as caught:
        LinuxBackend(runner).call("type", {"text": "hi"})
    assert caught.value.code == "unsupported_platform"
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(linux_module.shutil, "which", lambda name: None)
    with pytest.raises(EngineError, match="needs xdotool") as missing:
        LinuxBackend(runner).call("move", {"x": 1, "y": 2})
    assert "apt install xdotool" in (missing.value.hint or "")
    monkeypatch.setattr(linux_module, "atspi", lambda: None)
    with pytest.raises(EngineError, match="AT-SPI"):
        LinuxBackend(runner).call("inspect", {})
    assert runner.argv == []
    features = LinuxBackend(runner).op_capabilities()["features"]
    assert set(features) == set(FEATURES) and not features["pointer"]["available"]


def test_linux_clipboard_text_goes_through_a_private_file(x11: None) -> None:
    runner = Runner(default="")
    LinuxBackend(runner).call("clipboard_write", {"text": "secret"})
    argv = runner.argv[-1]
    assert argv[:3] == ["xclip", "-selection", "clipboard"] and "secret" not in argv
    assert not Path(argv[-1]).exists()  # removed after use


def test_linux_accessibility_tree_through_atspi(x11: None, monkeypatch: pytest.MonkeyPatch) -> None:
    class States:
        def __init__(self, *on: str) -> None:
            self.on = set(on)

        def contains(self, state: str) -> bool:
            return state in self.on

    class Node:
        def __init__(self, role: str, name: str, children: list[Any] = (), actions: int = 1) -> None:  # type: ignore[assignment]
            self.role, self.name, self.children, self.actions, self.done = role, name, list(children), actions, 0

        def get_role_name(self) -> str:
            return self.role

        def get_name(self) -> str:
            return self.name

        def get_child_count(self) -> int:
            return len(self.children)

        def get_child_at_index(self, index: int) -> Any:
            return self.children[index]

        def get_state_set(self) -> States:
            return States("enabled")

        def get_extents(self, _coords: Any) -> Any:
            return type("R", (), {"x": 10, "y": 20, "width": 30, "height": 40})()

        def get_text(self, _start: int, _end: int) -> str:
            return "typed secret"

        def get_n_actions(self) -> int:
            return self.actions

        def do_action(self, _index: int) -> None:
            self.done += 1

    save = Node("push button", "Save")
    password = Node("password text", "Password")
    app = Node("application", "gedit", [Node("frame", "Doc", [save, password])])
    api = type(
        "Atspi",
        (),
        {
            "get_desktop": staticmethod(lambda _n: Node("desktop", "", [app])),
            "CoordType": type("C", (), {"SCREEN": 0}),
            "StateType": type("S", (), {"ENABLED": "enabled", "FOCUSED": "focused"}),
        },
    )
    monkeypatch.setattr(linux_module, "atspi", lambda: api)
    backend = LinuxBackend(Runner())
    tree = backend.call("inspect", {"app": "gedit"})["elements"]
    assert {"role": "button", "name": "Save", "bounds": [10, 20, 30, 40]}.items() <= tree[0].items()
    assert tree[1]["secure"] and tree[1]["value"] == ""  # a password field's text is never read
    backend.call("click", {"name": "save", "role": "button", "app": "gedit"})
    assert save.done == 1


# ----------------------------------------------------------------- Windows
class FakeWin32:
    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.posted: list[tuple[int, int, int, int]] = []
        self.hwnds = [101, 102]

    def send(self, inputs: list[Any]) -> None:
        self.sent.append(inputs)

    def windows(self) -> list[int]:
        return self.hwnds

    def foreground(self) -> int:
        return 101

    def pid(self, hwnd: int) -> int:
        return hwnd * 10

    def process_name(self, pid: int) -> str:
        return {1010: "notepad", 1020: "WindowsTerminal"}[pid]

    def title(self, hwnd: int) -> str:
        return f"window {hwnd}"

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        return (0, 0, 800, 600)

    def post(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
        self.posted.append((hwnd, message, wparam, lparam))
        return True

    def metrics(self, index: int) -> int:
        return {76: 0, 77: 0, 78: 1920, 79: 1080}[index]

    def vk_for(self, char: str) -> tuple[int, bool]:
        return ord(char.upper()), char.isupper()

    def client_point(self, hwnd: int, x: int, y: int) -> tuple[int, int]:
        return x - 5, y - 5


def test_windows_keys_press_modifiers_around_the_key_and_command_means_ctrl() -> None:
    sequence = [(i.u.ki.wVk, i.u.ki.dwFlags) for i in key_inputs(0x54, ["command", "shift"])]
    assert sequence == [(0x11, 0), (0x10, 0), (0x54, 0), (0x54, KEYUP), (0x10, KEYUP), (0x11, KEYUP)]
    typed = [(i.u.ki.wScan, i.u.ki.dwFlags) for i in text_inputs("hé")]
    assert typed == [(ord("h"), UNICODE), (ord("h"), UNICODE | KEYUP), (ord("é"), UNICODE), (ord("é"), UNICODE | KEYUP)]
    api = FakeWin32()
    backend = WindowsBackend(Runner(), api=api)
    backend.call("hotkey", {"modifiers": ["command"], "key": "t"})
    backend.call("key", {"key": "enter"})
    assert [i.u.ki.wVk for i in api.sent[0]][:2] == [0x11, ord("T")]
    assert api.sent[1][0].u.ki.wVk == VK["enter"]


def test_windows_background_delivery_posts_messages_to_the_app() -> None:
    api = FakeWin32()
    backend = WindowsBackend(Runner(), api=api)
    backend.call("type", {"text": "ok", "app": "notepad"})
    assert api.posted == [(101, WM_CHAR, ord("o"), 0), (101, WM_CHAR, ord("k"), 0)] and api.sent == []
    with pytest.raises(EngineError, match="background"):
        backend.call("hotkey", {"modifiers": ["control"], "key": "s", "app": "notepad"})
    result = backend.call("click_at", {"x": 50, "y": 60, "app": "notepad"})
    assert result["background"] and api.posted[-1][3] == ((55 & 0xFFFF) << 16) | 45


def test_windows_ui_automation_runs_a_fixed_script_with_literal_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from highhx.automation.engine.platforms import windows as windows_module

    monkeypatch.setattr(windows_module, "uia_script", lambda: tmp_path / "highhx-uia.ps1")
    runner = Runner(default=json.dumps({"element": {"role": "button", "name": "Save"}, "pressed": True}))
    WindowsBackend(runner, api=FakeWin32()).call(
        "click", {"name": "Save'; Remove-Item C:\\ -Recurse #", "app": "notepad"}
    )
    argv = runner.argv[-1]
    assert argv[:7] == [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(tmp_path / "highhx-uia.ps1"),
    ]
    assert argv[7:] == ["click", "notepad", "Save'; Remove-Item C:\\ -Recurse #", "", "", ""]  # data, never code


def test_windows_window_listing_and_frames() -> None:
    api = FakeWin32()
    backend = WindowsBackend(Runner(), api=api)
    listed = backend.call("windows", {"app": "notepad.exe"})["windows"]
    assert [w["id"] for w in listed] == [101] and listed[0]["app"] == "notepad"
    with pytest.raises(EngineError) as caught:
        backend.call("window_frame", {"window": 999, "x": 0, "y": 0, "width": 10, "height": 10})
    assert caught.value.code == "not_found"


def test_every_backend_reports_every_feature_with_a_reason() -> None:
    for platform in ("darwin", "win32", "linux", "sunos5"):
        if platform == "darwin" and sys.platform != "darwin":
            continue
        backend = backend_for(Runner(), platform=platform)
        features = backend.op_capabilities()["features"]
        assert set(features) == set(FEATURES), platform
        assert all(isinstance(f["available"], bool) and f["detail"] for f in features.values()), platform
    with pytest.raises(EngineError) as caught:
        backend_for(Runner(), platform="sunos5").call("click_at", {"x": 1, "y": 1})
    assert caught.value.code == "unsupported_platform"


@pytest.mark.skipif(os.name == "nt", reason="the Windows API is only faked off Windows")
def test_the_windows_backend_never_pretends_off_windows() -> None:
    with pytest.raises(EngineError) as caught:
        WindowsBackend(Runner()).call("cursor", {})
    assert caught.value.code == "unsupported_platform"
