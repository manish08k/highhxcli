"""The computer-operations contract (protocol version 2): what each new operation accepts, where
screenshots may be written, which version each needs, the checked-in JSON export, and the
terminal guard for background input and menus."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from highhx.automation.engine.bridge import AutomationBridge, EngineError
from highhx.automation.engine.protocol import OPS, PROTOCOL_VERSION, ProtocolError, describe, validate
from highhx.utils.paths import user_data_dir
from tests.unit.automation.fakes import FakeEngine

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("op", "args", "problem"),
    [
        ("click_at", {"x": 1}, "needs y"),
        ("click_at", {"x": 1, "y": 2, "button": "left-ish"}, "must be one of"),
        ("click_at", {"x": 1, "y": 2, "count": 4}, "whole number"),
        ("click_at", {"x": 1.5, "y": 2}, "whole number"),
        ("click_at", {"x": 10**7, "y": 2}, "whole number"),
        ("click_at", {"x": True, "y": 2}, "whole number"),
        ("drag", {"from_x": 0, "from_y": 0, "to_x": 1}, "needs to_y"),
        ("drag", {"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1, "duration_ms": 60_000}, "whole number"),
        ("menu", {"app": "Notes", "path": []}, "1 to 8 names"),
        ("menu", {"app": "Notes", "path": "File > Save"}, "1 to 8 names"),
        ("menu", {"app": "Notes", "path": ["File", "\x07"]}, "control characters"),
        ("window_frame", {"window": 1, "x": 0, "y": 0, "width": 0, "height": 10}, "whole number"),
        ("screenshot", {"path": "/tmp/x.png"}, "directly in"),
        ("screenshot", {"path": "relative.png"}, "directly in"),
        ("quit", {"app": "Evil; rm -rf ~"}, "not an application name"),
        ("clipboard_write", {"text": "x" * 100_001}, "characters"),
        ("type", {"text": "hi", "app": "../bin/sh"}, "not an application name"),
        ("run_command", {"command": "ls"}, "unknown operation"),
    ],
)
def test_version_2_operations_refuse_what_they_do_not_accept(op: str, args: dict[str, Any], problem: str) -> None:
    with pytest.raises(ProtocolError, match=re.escape(problem)):
        validate(op, args)


def test_screenshots_are_written_only_into_highhx_screenshots_folder() -> None:
    folder = user_data_dir() / "screenshots"
    assert validate("screenshot", {"path": str(folder / "a.png")})["path"] == str(folder / "a.png")
    for bad in (folder / "a.jpg", folder / "sub" / "a.png", folder / ".." / "a.png", Path.home() / ".ssh" / "a.png"):
        with pytest.raises(ProtocolError):
            validate("screenshot", {"path": str(bad)})


def test_operations_say_which_version_they_need() -> None:
    assert OPS["click"].needs({"name": "Save"}) == 1
    assert OPS["click_at"].needs({"x": 1, "y": 2}) == 2
    assert OPS["scroll"].needs({"direction": "down"}) == 1
    assert OPS["scroll"].needs({"direction": "down", "x": 1, "y": 1}) == 2  # a version-2 argument
    assert OPS["type"].needs({"text": "a", "app": "Notes"}) == 2
    data = describe()
    assert data["version"] == PROTOCOL_VERSION == 2
    assert data["ops"]["scroll"]["args"]["x"]["since"] == 2 and data["ops"]["scroll"]["since"] == 1


def test_the_checked_in_contract_is_the_protocol() -> None:
    """schemas/computer-protocol.json is generated from the Python contract (scripts/generate_schemas.py)."""
    checked_in = json.loads((ROOT / "schemas" / "computer-protocol.json").read_text(encoding="utf-8"))
    assert checked_in == json.loads(json.dumps(describe())), "regenerate it: python scripts/generate_schemas.py"


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("type", {"text": "rm -rf ~", "app": "Terminal"}),
        ("key", {"key": "enter", "app": "iTerm2"}),
        ("hotkey", {"modifiers": ["cmd"], "key": "v", "app": "Ghostty"}),
        ("menu", {"app": "Terminal", "path": ["Edit", "Paste"]}),  # pasting is typing
    ],
)
def test_input_never_reaches_a_terminal_even_in_the_background(op: str, args: dict[str, Any]) -> None:
    fake = FakeEngine()  # Notes is in front: the named target is what counts
    with pytest.raises(EngineError) as caught:
        AutomationBridge(fake).call(op, **args)
    assert caught.value.code == "refused" and fake.sent(op) == []


def test_background_input_to_another_app_is_allowed() -> None:
    fake = FakeEngine()
    fake.front = "Terminal"  # the terminal is in front, but the input goes to Notes
    AutomationBridge(fake).call("type", text="hello", app="Notes")
    assert fake.sent("type") == [("type", {"text": "hello", "app": "Notes"})]
