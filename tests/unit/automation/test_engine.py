"""The automation bridge: a fixed, validated protocol; the terminal guard; engine selection; the
Python engine's commands; the .NET engine client (against a scripted engine process); and
the C# engine's protocol tables matching the Python ones."""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from highhx.automation.engine import bridge as bridge_module
from highhx.automation.engine.bridge import AutomationBridge, EngineError, open_bridge
from highhx.automation.engine.protocol import KEY_CODES, MODIFIERS, OPS, ROLES, ProtocolError, describe, validate
from highhx.automation.engine.python_engine import PythonEngine
from tests.unit.automation.fakes import FakeEngine

ROOT = Path(__file__).resolve().parents[3]
CSHARP = ROOT / "engine" / "dotnet" / "HighhX.Automation"


# ------------------------------------------------------------------ protocol
@pytest.mark.parametrize(
    ("op", "args", "problem"),
    [
        ("run_script", {}, "unknown operation"),
        ("shell", {"command": "rm -rf ~"}, "unknown operation"),
        ("click", {"x": 10, "y": 20}, "does not take"),  # never raw coordinates
        ("click", {}, "needs name"),
        ("launch", {"app": "Evil; rm -rf ~"}, "not an application name"),
        ("launch", {"app": "../../bin/sh"}, "not an application name"),
        ("open_url", {"url": "file:///etc/passwd"}, "http(s) URL"),
        ("open_url", {"url": "javascript:alert(1)"}, "http(s) URL"),
        ("key", {"key": "f13"}, "unknown key"),
        ("hotkey", {"modifiers": ["hyper"], "key": "t"}, "unknown modifier"),
        ("hotkey", {"modifiers": [], "key": "t"}, "1 to 3 modifiers"),
        ("type", {"text": "x" * 2001}, "characters"),
        ("type", {"text": "a\x1bb"}, "control characters"),
        ("scroll", {"direction": "sideways"}, "must be one of"),
        ("wait", {"ms": 60_000}, "whole number"),
        ("verify", {"check": "anything"}, "must be one of"),
    ],
)
def test_the_protocol_refuses_what_it_does_not_list(op: str, args: dict[str, Any], problem: str) -> None:
    with pytest.raises(ProtocolError, match=re.escape(problem)):
        validate(op, args)


def test_arguments_are_normalised() -> None:
    assert validate("hotkey", {"modifiers": ["cmd", "Shift"], "key": "T"}) == {
        "modifiers": ["command", "shift"],
        "key": "t",
    }
    assert validate("key", {"key": "Enter"}) == {"key": "enter"}
    assert validate("open_url", {"url": " https://mail.google.com/ "}) == {"url": "https://mail.google.com/"}
    assert set(describe()["ops"]) == set(OPS)


def test_keyboard_input_never_reaches_a_terminal() -> None:
    fake = FakeEngine()
    bridge = AutomationBridge(fake)
    assert bridge.call("type", text="hello") == {"op": "type", "text": "hello"}
    for terminal in ("Terminal", "iTerm2", "Warp", "Ghostty"):
        fake.front, fake.calls = terminal, []
        for op, args in (("type", {"text": "rm -rf ~"}), ("key", {"key": "enter"})):
            with pytest.raises(EngineError) as caught:
                bridge.call(op, **args)
            assert caught.value.code == "refused"
        assert fake.sent("type", "key") == []  # checked before anything was sent
    assert bridge.call("launch", app="Slack")["running"]  # non-keyboard operations are fine


# ------------------------------------------------------------------ selection
def test_engine_selection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def runner(argv: list[str], what: str) -> tuple[int, str, str]:
        return 0, "{}", ""

    monkeypatch.setattr(bridge_module, "engine_binary", lambda: None)
    monkeypatch.delenv(bridge_module.ENGINE_ENV, raising=False)
    assert open_bridge(runner).name == "python"  # nothing installed: the built-in engine
    monkeypatch.setenv(bridge_module.ENGINE_ENV, "dotnet")
    with pytest.raises(EngineError, match="not installed"):
        open_bridge(runner)
    monkeypatch.setenv(bridge_module.ENGINE_ENV, "python")
    assert open_bridge(runner).name == "python"


def test_a_broken_dotnet_install_falls_back_in_auto_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    broken = tmp_path / "highhx-automation"
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)
    monkeypatch.setattr(bridge_module, "engine_binary", lambda: broken)
    monkeypatch.delenv(bridge_module.ENGINE_ENV, raising=False)
    assert open_bridge(lambda argv, what: (0, "", "")).name == "python"


# -------------------------------------------------------------- python engine
class Recorder:
    def __init__(self, replies: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.argv: list[list[str]] = []
        self.replies = replies or {}

    def __call__(self, argv: list[str], what: str) -> tuple[int, str, str]:
        self.argv.append(argv)
        for marker, reply in self.replies.items():
            if marker in " ".join(argv):
                return reply
        return 0, '{"app": "Notes", "title": "", "running": true, "trusted": true}', ""


@pytest.mark.skipif(sys.platform != "darwin", reason="the Python engine's desktop operations are macOS-only")
def test_python_engine_passes_user_text_as_arguments_only() -> None:
    runner = Recorder()
    engine = PythonEngine(runner)
    engine.call("type", {"text": '\'); do shell script "rm -rf ~" --'})
    script, *args = runner.argv[-1][4:]
    assert "rm -rf" not in script and args == ['\'); do shell script "rm -rf ~" --']
    engine.call("hotkey", {"modifiers": ["command"], "key": "t"})
    assert runner.argv[-1][-2:] == ["t", '["command"]']
    engine.call("open_url", {"url": "https://github.com", "app": "Safari"})
    assert runner.argv[-1] == ["open", "-a", "Safari", "https://github.com"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
def test_python_engine_reports_missing_accessibility_clearly() -> None:
    runner = Recorder({"keystroke": (1, "", "osascript is not allowed assistive access. (-25211)")})
    with pytest.raises(EngineError) as caught:
        PythonEngine(runner).call("type", {"text": "hi"})
    assert caught.value.code == "accessibility_denied"
    assert "Privacy & Security → Accessibility" in (caught.value.hint or "")


def test_python_engine_is_honest_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    engine = PythonEngine(Recorder())
    with pytest.raises(EngineError) as caught:
        engine.call("type", {"text": "hi"})
    assert caught.value.code == "unsupported_platform"
    assert engine.call("status", {})["ok"] is False


# ---------------------------------------------------------------- .NET client
FAKE_DOTNET = textwrap.dedent(
    """
    import json, sys
    protocol = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    for line in sys.stdin:
        req = json.loads(line)
        op, args = req["op"], req["args"]
        if op == "status":  # the handshake: answered at any version
            out = {"ok": True, "result": {"engine": "dotnet", "protocol": protocol, "version": "1.0.0", "ok": True}}
        elif req["v"] != protocol:  # like the C# engine: every other request at its own version
            out = {"ok": False, "error": {"code": "invalid_request", "message": f"protocol version {protocol} expected"}}
        elif op == "focus":
            out = {"ok": False, "error": {"code": "accessibility_denied", "message": "not trusted"}}
        elif op == "launch":
            out = {"ok": False, "error": {"code": "not_found", "message": "no such app"}}
        else:
            out = {"ok": True, "result": {"echo": op, "args": args}}
        print(json.dumps({"v": 1, "id": req["id"], **out}), flush=True)
    """
)


def _fake_engine(tmp_path: Path, protocol: int = 1) -> Path:
    script = tmp_path / "engine.py"
    script.write_text(FAKE_DOTNET)
    binary = tmp_path / "highhx-automation"
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$1" {protocol}\n')
    binary.chmod(0o755)
    return binary


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses a POSIX shell script as the engine")
def test_dotnet_client_speaks_the_protocol(tmp_path: Path) -> None:
    from highhx.automation.engine.dotnet_engine import DotnetEngine

    engine = DotnetEngine(_fake_engine(tmp_path))
    try:
        assert engine.version == "1.0.0"
        bridge = AutomationBridge(engine)
        assert bridge.call("scroll", direction="down", amount=2) == {
            "echo": "scroll",
            "args": {"direction": "down", "amount": 2},
        }
        with pytest.raises(EngineError) as denied:
            bridge.call("focus", app="Slack")
        assert denied.value.code == "accessibility_denied" and denied.value.hint
        with pytest.raises(EngineError) as missing:
            bridge.call("launch", app="Nope")
        assert missing.value.code == "not_found"
    finally:
        engine.close()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses a POSIX shell script as the engine")
def test_dotnet_client_refuses_another_protocol_version(tmp_path: Path) -> None:
    from highhx.automation.engine.dotnet_engine import DotnetEngine

    for version in (0, 3):  # older than HighhX still speaks, or newer than it knows
        with pytest.raises(EngineError, match=f"protocol {version}"):
            DotnetEngine(_fake_engine(tmp_path, protocol=version))


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses a POSIX shell script as the engine")
def test_a_protocol_1_engine_keeps_working_and_newer_ops_go_to_the_built_in_engine(tmp_path: Path) -> None:
    from highhx.automation.engine.dotnet_engine import DotnetEngine

    engine = DotnetEngine(_fake_engine(tmp_path, protocol=1))
    fallback = FakeEngine()
    try:
        assert engine.protocol == 1
        bridge = AutomationBridge(engine, fallback=fallback)
        assert bridge.call("scroll", direction="down")["echo"] == "scroll"  # version 1: the .NET engine
        bridge.call("screen")  # version 2: the built-in engine
        bridge.call("scroll", direction="down", x=10, y=20)  # a version-2 argument: the built-in engine
        assert [op for op, _ in fallback.calls] == ["screen", "scroll"]
        with pytest.raises(EngineError) as unsupported:
            AutomationBridge(engine).call("screen")  # nothing newer to fall back to: said, not faked
        assert unsupported.value.code == "unsupported" and "protocol 2" in unsupported.value.message
    finally:
        engine.close()


# ------------------------------------------------------------ C# conformance
def _csharp_list(source: str, name: str) -> set[str]:
    match = re.search(rf"{name}\s*=\s*\[(.*?)\];", source, re.S)
    assert match, name
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_the_csharp_engine_mirrors_the_protocol() -> None:
    source = (CSHARP / "Protocol.cs").read_text(encoding="utf-8")
    assert "public const int Version = 1;" in source
    version_1 = {name for name, op in OPS.items() if op.since == 1}  # the version the C# engine declares
    assert _csharp_list(source, "Ops") == version_1
    assert _csharp_list(source, "KeyboardOps") == {name for name in version_1 if OPS[name].keyboard}
    assert _csharp_list(source, "Modifiers") == set(MODIFIERS.values())
    assert _csharp_list(source, "Roles") == set(ROLES)
    assert _csharp_list(source, "TerminalApps") == set(bridge_module.TERMINAL_APPS)
    keys = dict(re.findall(r'\["([a-z]+)"\]\s*=\s*(\d+)', source.split("KeyCodes", 1)[1].split("};", 1)[0]))
    assert {k: int(v) for k, v in keys.items()} == KEY_CODES


def test_the_csharp_engine_has_no_escape_hatches() -> None:
    code = "\n".join(p.read_text(encoding="utf-8") for p in CSHARP.glob("*.cs"))
    for forbidden in (
        "/bin/sh",
        "bash",
        "osascript",
        "NSAppleScript",
        "UseShellExecute = true",
        "CGEventCreateMouseEvent",
    ):
        assert forbidden not in code, forbidden
    assert code.count('new ProcessStartInfo("/usr/bin/open")') == 1  # the only process it starts
