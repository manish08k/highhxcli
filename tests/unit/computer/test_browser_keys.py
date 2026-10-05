"""Browser keys and combinations: parsing, the events Chrome receives (macOS editing commands),
the policy seeing through modifiers, and invalid keys refused at every entry point. Real Chrome:
tests/unit/computer/test_live_browser.py (HIGHHX_TEST_BROWSER=1)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from highhx.approvals.risk import RiskLevel
from highhx.computer.browser import KEY_CODES, key_event, parse_key
from highhx.core.errors import IntegrationError, ValidationError
from highhx.safety.actions import ActionDescriptor, ActionKind, attrs
from highhx.safety.classifier import SUBMIT, SafetyPolicy


def test_keys_and_combinations_parse_to_canonical_modifiers() -> None:
    assert parse_key("enter") == ([], "enter")
    assert parse_key("Shift+Tab") == (["shift"], "tab")
    assert parse_key("cmd+shift+z") == (["command", "shift"], "z")
    assert parse_key("shift+ctrl+alt+k") == (["option", "control", "shift"], "k")  # one fixed order
    assert parse_key("A") == ([], "A") and parse_key("+") == ([], "+") and parse_key("ctrl++") == (["control"], "+")
    for bad in ("hyper+a", "enterr", "ctrl+", "f13"):
        with pytest.raises(IntegrationError, match="Unsupported key"):
            parse_key(bad)


def test_events_carry_modifiers_text_and_on_macos_the_editing_command() -> None:
    down, up = key_event(["shift"], "a", mac=False)
    assert (down["key"], down["code"], down["text"], down["modifiers"]) == ("A", "KeyA", "A", 8)
    assert up["type"] == "keyUp" and "text" not in up
    down, _ = key_event(["shift"], "1", mac=False)
    assert (down["key"], down["code"], down["text"]) == ("!", "Digit1", "!")
    down, _ = key_event(["command"], "a", mac=True)
    assert down["commands"] == ["selectAll"] and "text" not in down and down["modifiers"] == 4
    assert "commands" not in key_event(["command"], "a", mac=False)[0]  # elsewhere the page handles it
    assert "commands" not in key_event(["control"], "a", mac=True)[0]  # ctrl+a is not select-all on a Mac
    assert key_event(["command", "shift"], "z", mac=True)[0]["commands"] == ["redo"]
    down, _ = key_event([], "forwarddelete", mac=False)
    assert (down["key"], down["windowsVirtualKeyCode"], down["type"]) == ("Delete", 46, "rawKeyDown")
    assert key_event([], "enter", mac=False)[0]["text"] == "\r"
    assert {f"f{n}" for n in range(1, 13)} <= set(KEY_CODES)


@pytest.mark.parametrize("key", ["enter", "ctrl+enter", "cmd+enter", "shift+Enter", "return"])
def test_enter_with_any_modifiers_in_a_form_is_a_submit(key: str) -> None:
    def press(in_form: bool) -> ActionDescriptor:
        return ActionDescriptor(
            ActionKind.UI_KEY, f"press {key}", "computer", target="Email", attributes=attrs(key=key, in_form=in_form)
        )

    verdict = SafetyPolicy().classify(press(True))
    assert SUBMIT in verdict.categories and verdict.risk == RiskLevel.DANGEROUS
    assert SUBMIT not in SafetyPolicy().classify(press(False)).categories


def test_desktop_combinations_that_delete_or_submit_are_asked_first() -> None:
    from highhx.actions.catalog import default_catalog
    from highhx.actions.policy import Risk

    press = next(spec for spec in default_catalog() if spec.name == "computer.press")
    assert press.risk_for is not None
    for key in ("cmd+enter", "shift+forwarddelete", "option+backspace"):
        assert press.risk_for({"key": key}) == Risk.MEDIUM, key
    assert press.risk_for({"key": "shift+tab"}) == Risk.LOW


def test_a_flow_with_an_unknown_key_is_refused_when_loaded(tmp_path: Path) -> None:
    from highhx.computer.flows import load_flow

    good = tmp_path / "good.yaml"
    good.write_text("name: g\nsteps:\n  - press: shift+tab\n  - press: cmd+a\n  - press: f5\n")
    assert [s["press"] for s in load_flow(good).steps] == ["shift+tab", "cmd+a", "f5"]
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: b\nsteps:\n  - press: hyper+q\n")
    with pytest.raises(ValidationError) as caught:
        load_flow(bad)
    assert "unknown modifier 'hyper'" in str(caught.value.details)


def test_the_cli_refuses_an_unknown_key_before_touching_a_browser() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "highhx", "computer", "press", "enterr"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0 and "Unsupported key 'enterr'" in result.stdout + result.stderr
