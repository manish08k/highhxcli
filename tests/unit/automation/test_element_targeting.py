"""Exact element targeting: a press reaches the element that was observed, or is refused.

The computer runtime binds an approved action to one observed element. Engines find elements by
name, so the observed occurrence (``index``) and position (``bounds``) travel with the name:
several same-named elements without an index are ``ambiguous_target``, and an element that is gone
or has moved is ``stale_target`` — never a press on a different control that shares the name."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from highhx.automation.engine.bridge import AutomationBridge, EngineError
from highhx.automation.engine.platforms import BOUNDS_TOLERANCE, choose_element
from highhx.automation.engine.platforms import linux as linux_module
from highhx.automation.engine.platforms.linux import LinuxBackend
from highhx.automation.engine.platforms.macos import MacBackend
from highhx.automation.engine.platforms.windows import WindowsBackend
from highhx.automation.engine.protocol import ProtocolError, validate
from highhx.automation.engine.provider import BridgeDesktopProvider
from highhx.computer.driver import HighhXDriver
from highhx.computer.model import Observation, UIElement
from tests.computer_use.environment import SimulatedDesktop
from tests.unit.automation.test_platform_backends import FakeWin32, Runner

TWO_OKS = [
    ("first", "button", "OK", [10, 10, 40, 20]),
    ("cancel", "button", "Cancel", [60, 10, 40, 20]),
    ("second", "button", "OK", [10, 200, 40, 20]),
    ("label", "text", "OK", [0, 0, 5, 5]),
]


def code(call: Any) -> str:
    with pytest.raises(EngineError) as info:
        call()
    return info.value.code


# ------------------------------------------------------------- the rule
def test_one_exact_match_needs_no_index() -> None:
    assert choose_element(TWO_OKS, "cancel", "button") == "cancel"


def test_several_exact_matches_without_an_index_are_ambiguous_never_the_first() -> None:
    assert code(lambda: choose_element(TWO_OKS, "OK", "button")) == "ambiguous_target"


def test_the_index_picks_the_observed_occurrence() -> None:
    assert choose_element(TWO_OKS, "ok", "button", index=0) == "first"
    assert choose_element(TWO_OKS, "ok", "button", index=1, bounds=[10, 200, 40, 20]) == "second"
    assert choose_element(TWO_OKS, "ok", "any", index=2) == "label"  # "any" counts every role


def test_a_vanished_occurrence_is_stale() -> None:
    assert code(lambda: choose_element(TWO_OKS, "OK", "button", index=2)) == "stale_target"


def test_a_moved_element_is_stale_within_a_small_tolerance() -> None:
    moved = [10, 200 + BOUNDS_TOLERANCE + 1, 40, 20]
    assert code(lambda: choose_element(TWO_OKS, "OK", "button", index=1, bounds=moved)) == "stale_target"
    nudged = [10 + BOUNDS_TOLERANCE, 200, 40, 20]
    assert choose_element(TWO_OKS, "OK", "button", index=1, bounds=nudged) == "second"


def test_partial_names_are_used_only_when_unique() -> None:
    assert choose_element(TWO_OKS, "canc", None) == "cancel"
    assert code(lambda: choose_element(TWO_OKS, "o", "button")) == "ambiguous_target"
    assert code(lambda: choose_element(TWO_OKS, "Delete", None)) == "not_found"


def test_the_protocol_checks_index_and_bounds() -> None:
    clean = validate("click", {"name": "OK", "index": 1, "bounds": [10, -5, 40, 20]})
    assert clean["index"] == 1 and clean["bounds"] == [10, -5, 40, 20]
    for bad in ({"bounds": [1, 2, 3]}, {"bounds": [1, 2, -3, 4]}, {"bounds": "1,2,3,4"}, {"index": -1}):
        with pytest.raises(ProtocolError):
            validate("click", {"name": "OK", **bad})


# ----------------------------------------------------------- the engines
def test_macos_presses_the_observed_occurrence(monkeypatch: pytest.MonkeyPatch) -> None:
    pressed: list[str] = []
    elements = [
        UIElement(id="a1", role="button", name="OK", bounds=(10, 10, 40, 20)),
        UIElement(id="a2", role="button", name="OK", bounds=(10, 200, 40, 20)),
    ]
    provider = type("P", (), {"click": lambda self, eid, cancel=None: pressed.append(eid)})()
    backend = MacBackend(Runner())
    monkeypatch.setattr(backend, "_observe", lambda app: (provider, Observation("ax", "Notes", elements=elements)))
    backend.call("click", {"name": "OK", "role": "button", "index": 1, "bounds": [10, 200, 40, 20]})
    assert pressed == ["a2"]
    assert code(lambda: backend.call("click", {"name": "OK", "role": "button"})) == "ambiguous_target"
    assert code(lambda: backend.call("click", {"name": "OK", "index": 0, "bounds": [10, 90, 40, 20]})) == "stale_target"
    assert pressed == ["a2"]  # refusals press nothing
    assert backend.call("verify", {"check": "element", "name": "OK"})["ok"]  # existence is not ambiguity


def test_linux_presses_the_observed_occurrence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    class Node:
        def __init__(self, role: str, name: str, y: int = 0, children: list[Any] | None = None) -> None:
            self.role, self.name, self.y, self.children, self.done = role, name, y, children or [], 0

        def get_role_name(self) -> str:
            return self.role

        def get_name(self) -> str:
            return self.name

        def get_child_count(self) -> int:
            return len(self.children)

        def get_child_at_index(self, index: int) -> Any:
            return self.children[index]

        def get_state_set(self) -> Any:
            return type("S", (), {"contains": lambda self, s: s == "enabled"})()

        def get_extents(self, _coords: Any) -> Any:
            return type("R", (), {"x": 10, "y": self.y, "width": 40, "height": 20})()

        def get_text(self, _start: int, _end: int) -> str:
            return ""

        def get_n_actions(self) -> int:
            return 1

        def do_action(self, _index: int) -> None:
            self.done += 1

    first, second = Node("push button", "OK", 10), Node("push button", "OK", 200)
    app = Node("application", "gedit", children=[Node("frame", "Doc", children=[first, Node("filler", "OK"), second])])
    api = type(
        "Atspi",
        (),
        {
            "get_desktop": staticmethod(lambda _n: Node("desktop", "", children=[app])),
            "CoordType": type("C", (), {"SCREEN": 0}),
            "StateType": type("S", (), {"ENABLED": "enabled", "FOCUSED": "focused"}),
        },
    )
    monkeypatch.setattr(linux_module, "atspi", lambda: api)
    backend = LinuxBackend(Runner())
    tree = backend.call("inspect", {"app": "gedit"})["elements"]
    assert [e["bounds"][1] for e in tree if e["name"] == "OK"] == [10, 200]  # the order click counts in
    backend.call("click", {"name": "OK", "role": "button", "app": "gedit", "index": 1, "bounds": [10, 200, 40, 20]})
    assert (first.done, second.done) == (0, 1)
    assert code(lambda: backend.call("click", {"name": "OK", "role": "button", "app": "gedit"})) == "ambiguous_target"
    assert code(lambda: backend.call("click", {"name": "OK", "app": "gedit", "index": 2})) == "stale_target"
    assert (first.done, second.done) == (0, 1)


def test_windows_sends_index_and_bounds_as_literal_arguments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from highhx.automation.engine.platforms import windows as windows_module

    monkeypatch.setattr(windows_module, "uia_script", lambda: tmp_path / "highhx-uia.ps1")
    runner = Runner(default=json.dumps({"element": {"role": "button", "name": "OK"}, "pressed": True}))
    backend = WindowsBackend(runner, api=FakeWin32())
    backend.call("click", {"name": "OK", "role": "button", "app": "notepad", "index": 1, "bounds": [10, 200, 40, 20]})
    assert runner.argv[-1][7:] == ["click", "notepad", "OK", "button", "1", "10,200,40,20"]
    for reply, expected in (("stale", "stale_target"), ("moved", "stale_target"), ("ambiguous", "ambiguous_target")):
        runner.default = json.dumps({"error": reply, "names": ["OK", "OK"]})
        assert code(lambda: backend.call("click", {"name": "OK", "app": "notepad"})) == expected


def test_the_windows_script_applies_the_same_rule() -> None:
    from highhx.automation.engine.platforms.windows import UIA_SCRIPT

    click = UIA_SCRIPT[UIA_SCRIPT.index("'click' {") : UIA_SCRIPT.index("'at' {")]
    assert "$exact[[int]$D]" in click and "error = 'stale'" in click and "error = 'ambiguous'" in click
    assert f"-gt {BOUNDS_TOLERANCE})" in click and "@@" not in UIA_SCRIPT


# ------------------------------------------- the runtime's provider, end to end
def desktop_with_two_oks() -> SimulatedDesktop:
    return SimulatedDesktop(
        {
            "front": "Dialogs",
            "apps": {
                "Dialogs": {
                    "window": {
                        "id": 1,
                        "pid": 10,
                        "app": "Dialogs",
                        "title": "Two",
                        "x": 0,
                        "y": 0,
                        "width": 400,
                        "height": 400,
                    },
                    "elements": [
                        {"role": "button", "name": "OK", "bounds": [10, 10, 40, 20]},
                        {"role": "button", "name": "OK", "bounds": [10, 200, 40, 20]},
                    ],
                }
            },
        }
    )


def test_the_provider_presses_the_element_the_runtime_bound() -> None:
    env = desktop_with_two_oks()
    provider = BridgeDesktopProvider(HighhXDriver(AutomationBridge(env)), "Dialogs")
    second = provider.observe().elements[1]
    provider.click(second.id)
    first_ok, second_ok = env.apps["Dialogs"]["elements"]
    assert (first_ok.get("presses", 0), second_ok.get("presses", 0)) == (0, 1)


def test_the_provider_refuses_when_the_ui_changed_after_observing() -> None:
    env = desktop_with_two_oks()
    provider = BridgeDesktopProvider(HighhXDriver(AutomationBridge(env)), "Dialogs")
    second = provider.observe().elements[1]
    env.apps["Dialogs"]["elements"][1]["bounds"] = [10, 320, 40, 20]  # the dialog re-laid out
    assert code(lambda: provider.click(second.id)) == "stale_target"
    env.apps["Dialogs"]["elements"].pop()  # and then the second OK disappeared
    assert code(lambda: provider.click(second.id)) == "stale_target"
    assert all(e.get("presses", 0) == 0 for e in env.apps["Dialogs"]["elements"])
