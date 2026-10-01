"""Mouse button down/up, bringing one exact window to the front, and region / application-window
screenshots: each backend's native call, the protocol's checks, the actions (approval and
verification), and the same operations through the CLI, the Pro agent's computer_act and MCP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.policy import Approval
from highhx.agent.tools.computer import desktop_operation
from highhx.automation.engine.bridge import EngineError
from highhx.automation.engine.platforms import quartz
from highhx.automation.engine.platforms.linux import LinuxBackend
from highhx.automation.engine.platforms.macos import MacBackend
from highhx.automation.engine.platforms.windows import MOUSE_FLAGS, WindowsBackend
from highhx.automation.engine.protocol import ProtocolError, validate
from highhx.computer.mcp import TOOLS
from tests.unit.automation.fakes import FakeEngine
from tests.unit.automation.test_platform_backends import FakeWin32, Runner, fake_quartz, x11  # noqa: F401

NOTES = {"id": 7, "pid": 70, "app": "Notes", "title": "a", "x": 0, "y": 25, "width": 800, "height": 600}
MAIL = {"id": 9, "pid": 90, "app": "Mail", "title": "b", "x": 50, "y": 50, "width": 400, "height": 300}


# ------------------------------------------------------------------ protocol
def test_the_protocol_checks_the_new_operations() -> None:
    assert validate("mouse_button", {"action": "DOWN", "x": 1, "y": 2})["action"] == "down"
    assert validate("screenshot", {"path": _shot_path(), "region": [0, 25, 100, 50]})["region"] == [0, 25, 100, 50]
    for op, args in (
        ("mouse_button", {"action": "hold", "x": 1, "y": 2}),
        ("mouse_button", {"action": "down", "x": 1}),
        ("window_focus", {"window": -1}),
        ("screenshot", {"path": _shot_path(), "window": 3, "region": [0, 0, 1, 1]}),  # one or the other
    ):
        with pytest.raises(ProtocolError):
            validate(op, args)


def _shot_path() -> str:
    from highhx.utils.paths import user_data_dir

    folder = user_data_dir() / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder / "t.png")


# ------------------------------------------------------------------- macOS
def test_mac_button_down_and_up(fake_quartz: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    pressed: list[tuple[int, int, str, bool]] = []
    monkeypatch.setattr(quartz, "press_button", lambda x, y, button, down: pressed.append((x, y, button, down)))
    backend = MacBackend(Runner())
    backend.call("mouse_button", {"action": "down", "x": 10, "y": 20})
    backend.call("mouse_button", {"action": "up", "x": 30, "y": 40, "button": "right"})
    assert pressed == [(10, 20, "left", True), (30, 40, "right", False)]
    fake_quartz["trusted"] = False
    with pytest.raises(EngineError) as info:
        backend.call("mouse_button", {"action": "down", "x": 1, "y": 1})
    assert info.value.code == "accessibility_denied" and len(pressed) == 2  # refused, not dropped


def test_mac_window_focus_raises_the_exact_window(
    fake_quartz: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = [dict(MAIL, id=5), {**NOTES, "id": 6}]
    monkeypatch.setattr(quartz, "windows", lambda: order)
    raised: list[tuple[int, tuple[int, int, int, int]]] = []

    def raise_window(pid: int, match: tuple[int, int, int, int]) -> bool:
        raised.append((pid, match))
        order.insert(0, order.pop(1))  # the window server moves it to the front
        return True

    monkeypatch.setattr(quartz, "raise_window", raise_window)
    runner = Runner()
    backend = MacBackend(runner)
    result = backend.call("window_focus", {"window": 6})
    assert result == {"window": 6, "app": "Notes", "frontmost": True}
    assert raised == [(70, (0, 25, 800, 600))] and "Notes" in " ".join(runner.argv[-1])  # raised, then activated
    with pytest.raises(EngineError) as info:
        backend.call("window_focus", {"window": 999})
    assert info.value.code == "not_found"


def test_mac_region_screenshot(fake_quartz: dict[str, Any]) -> None:  # noqa: F811
    runner = Runner()
    result = MacBackend(runner).call("screenshot", {"path": _shot_path(), "region": [10, 20, 300, 200]})
    assert "-R" in runner.argv[-1] and "10,20,300,200" in runner.argv[-1] and result["region"] == [10, 20, 300, 200]


# ----------------------------------------------------------------- Windows
def test_windows_button_down_up_and_window_focus() -> None:
    api = FakeWin32()
    activated: list[int] = []
    api.activate = activated.append  # type: ignore[attr-defined]
    backend = WindowsBackend(Runner(), api=api)
    backend.call("mouse_button", {"action": "down", "x": 5, "y": 6})
    backend.call("mouse_button", {"action": "up", "x": 5, "y": 6, "button": "right"})
    flags = [[i.u.mi.dwFlags for i in batch] for batch in api.sent]
    assert flags[0][1] == MOUSE_FLAGS["left"][0] and flags[1][1] == MOUSE_FLAGS["right"][1]  # after a move each
    assert backend.call("window_focus", {"window": 101}) == {"window": 101, "app": "notepad", "frontmost": True}
    assert backend.call("window_focus", {"window": 102})["frontmost"] is False  # the foreground stayed 101
    assert activated == [101, 102]
    with pytest.raises(EngineError):
        backend.call("window_focus", {"window": 555})


def test_windows_region_screenshot_captures_that_area(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from highhx.automation.engine.platforms import windows as windows_module

    monkeypatch.setattr(windows_module, "uia_script", lambda: tmp_path / "s.ps1")
    runner = Runner(default=json.dumps({"ok": True}))

    def capture(argv: list[str], what: str) -> tuple[int, str, str]:
        Path(argv[8]).write_bytes(_png())
        return runner(argv, what)

    WindowsBackend(capture, api=FakeWin32()).call("screenshot", {"path": _shot_path(), "region": [1, 2, 30, 40]})
    assert runner.argv[-1][7:] == ["capture", _shot_path(), "1", "2", "30", "40", ""]


def _png() -> bytes:
    from tests.unit.automation.fakes import PNG_1X1

    return PNG_1X1


# ------------------------------------------------------------------- Linux
def test_linux_button_down_up_and_window_focus(x11: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    runner = Runner({"search": (0, "4242\n", ""), "getactivewindow": (0, "4242\n", "")})
    backend = LinuxBackend(runner)
    monkeypatch.setattr(backend, "_window", lambda wid: {"app": "gedit"})
    backend.call("mouse_button", {"action": "down", "x": 5, "y": 6})
    backend.call("mouse_button", {"action": "up", "x": 5, "y": 6, "button": "right"})
    tails = [a[1:] for a in runner.argv]
    assert ["mousedown", "1"] in tails and ["mouseup", "3"] in tails
    assert backend.call("window_focus", {"window": 4242}) == {"window": 4242, "app": "gedit", "frontmost": True}
    assert ["windowactivate", "--sync", "4242"] in [a[1:] for a in runner.argv]
    with pytest.raises(EngineError) as info:
        backend.call("window_focus", {"window": 1})
    assert info.value.code == "not_found"


@pytest.mark.parametrize(
    ("tool", "crop"),
    [
        ("grim", ["-g", "10,20 300x200"]),
        ("import", ["-crop", "300x200+10+20", "+repage"]),
        ("scrot", ["-a", "10,20,300,200"]),
    ],
)
def test_linux_region_screenshot_per_tool(
    x11: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    crop: list[str],
) -> None:
    runner = Runner()
    backend = LinuxBackend(runner)
    monkeypatch.setattr(backend, "_capture_tool", lambda: tool)
    path = _shot_path()

    def capture(argv: list[str], what: str) -> tuple[int, str, str]:
        Path(argv[-1]).write_bytes(_png())
        return runner(argv, what)

    backend.runner = capture
    backend.call("screenshot", {"path": path, "region": [10, 20, 300, 200]})
    argv = runner.argv[-1]
    assert argv[0] == tool and argv[-1] == path
    assert any(argv[i : i + len(crop)] == crop for i in range(len(argv)))


def test_linux_region_needs_a_tool_that_can_crop(x11: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    backend = LinuxBackend(Runner())
    monkeypatch.setattr(backend, "_capture_tool", lambda: "gnome-screenshot")
    with pytest.raises(EngineError) as info:
        backend.call("screenshot", {"path": _shot_path(), "region": [0, 0, 5, 5]})
    assert info.value.code == "unsupported_platform"


# ------------------------------------------------------------------ actions
def test_button_actions_ask_and_verify_the_pointer(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    executor, ui = executor_for(agent_project)
    planned = executor.plan("computer.mouse_button", {"action": "down", "x": 3, "y": 4})
    assert planned.decision.approval == Approval.ASK  # like drag: always asked
    ui.default_action_answer = False
    assert executor.run("computer.mouse_button", {"action": "down", "x": 3, "y": 4}).status == "denied"
    assert engine.sent("mouse_button") == []
    ui.default_action_answer = True
    result = executor.run("computer.mouse_button", {"action": "down", "x": 3, "y": 4})
    assert result.ok and result.verified is None and result.output["cursor"] == [3, 4]
    assert executor.run("computer.mouse_button", {"action": "up", "x": 3, "y": 4}).ok
    assert [a["action"] for _op, a in engine.sent("mouse_button")] == ["down", "up"]


def test_focusing_one_window_is_verified(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    engine.windows = [dict(NOTES), dict(MAIL)]
    executor, _ = executor_for(agent_project)
    result = executor.run("computer.focus", {"window": 9})
    assert result.ok and result.verified and [w["id"] for w in engine.windows] == [9, 7]
    engine.focus_fails = True
    failed = executor.run("computer.focus", {"window": 7})
    assert not failed.ok and failed.verified is False and "window 7 is not in front" in failed.error
    with pytest.raises(Exception, match="Invalid input"):
        executor.run("computer.focus", {"app": "Notes", "window": 7})  # one or the other


def test_screenshots_of_an_application_window_or_a_region(
    agent_project: Path, executor_for, engine: FakeEngine
) -> None:
    engine.windows = [dict(NOTES), dict(MAIL)]
    executor, _ = executor_for(agent_project)
    of_mail = executor.run("computer.screenshot", {"app": "Mail"})
    assert of_mail.ok and engine.sent("screenshot")[-1][1]["window"] == 9 and of_mail.output["window"] == 9
    region = executor.run("computer.screenshot", {"region": [0, 25, 100, 50]})
    assert region.ok and engine.sent("screenshot")[-1][1]["region"] == [0, 25, 100, 50]
    assert not executor.run("computer.screenshot", {"app": "Mail", "region": [0, 0, 1, 1]}).ok
    assert not executor.run("computer.screenshot", {"app": "Calendar"}).ok  # no window: said, not faked


def test_verify_without_a_target_checks_the_frontmost_window(
    agent_project: Path, executor_for, engine: FakeEngine
) -> None:
    engine.windows = [dict(MAIL), dict(NOTES)]
    engine.front = "Mail"
    executor, _ = executor_for(agent_project)
    result = executor.run("computer.verify", {"expect": [{"window": {"exists": True}}], "timeout_ms": 0})
    assert result.ok and result.output["window"]["id"] == 9


# ------------------------------------------------ the agent, the CLI and MCP
def test_the_agent_reaches_the_same_actions() -> None:
    assert desktop_operation("mouse_down:3,4", None, None) == (
        "computer.mouse_button",
        {"action": "down", "x": 3, "y": 4},
    )
    assert desktop_operation("mouse_up:3,4", None, None)[1]["action"] == "up"  # type: ignore[index]
    assert desktop_operation("focus_window:9", None, None) == ("computer.focus", {"window": 9})
    assert desktop_operation("screenshot", None, "Mail") == ("computer.screenshot", {"app": "Mail"})
    verify = desktop_operation("verify", '[{"window": {"exists": true}}]', "Mail")
    assert verify == ("computer.verify", {"expect": [{"window": {"exists": True}}], "timeout_ms": 2000, "app": "Mail"})
    with pytest.raises(Exception, match="JSON"):
        desktop_operation("verify", "not json", None)


def test_the_cli_and_mcp_reach_the_same_actions(cli: Any, tmp_path: Path, engine: FakeEngine) -> None:
    engine.windows = [dict(NOTES), dict(MAIL)]
    assert cli("computer", "mouse", "down", "3,4", cwd=tmp_path).code != 0  # asks; no terminal, no --yes
    assert cli("--yes", "computer", "mouse", "down", "3,4", cwd=tmp_path).code == 0
    assert cli("--yes", "computer", "mouse", "up", "3,4", "--right", cwd=tmp_path).code == 0
    assert [a["button"] for _o, a in engine.sent("mouse_button")] == ["left", "right"]
    assert cli("computer", "window", "--id", "9", "--focus", cwd=tmp_path).code == 0
    assert engine.windows[0]["id"] == 9
    assert cli("computer", "window", "--focus", cwd=tmp_path).code != 0
    assert cli("computer", "screenshot", "--region", "0,25,100,50", cwd=tmp_path).code == 0
    assert cli("computer", "screenshot", "--app", "Notes", "--region", "0,0,1,1", cwd=tmp_path).code != 0
    assert "mouse_button" in TOOLS
