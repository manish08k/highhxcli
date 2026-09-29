"""`highhx computer …` commands for the HighhX Computer Runtime: each goes through the action
executor (approval, verification, audit) and the driver, and the contract export is exact."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from highhx.automation.engine.protocol import describe
from tests.unit.automation.fakes import FakeEngine, engine  # noqa: F401


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path


def test_windows_apps_and_element_at(cli: Any, project: Path, engine: FakeEngine) -> None:  # noqa: F811
    windows = cli("--json", "computer", "windows", cwd=project)
    assert windows.code == 0 and windows.json()["output"]["windows"][0]["id"] == 7
    apps = cli("--json", "computer", "windows", "--apps", cwd=project)
    assert {a["name"] for a in apps.json()["output"]["apps"]} == {"Notes", "Finder"}
    at = cli("--json", "computer", "at", "110,110", cwd=project)
    assert at.json()["output"]["name"] == "Save"
    bad = cli("computer", "at", "110", cwd=project)
    assert bad.code != 0 and "X,Y" in bad.stderr


def test_click_at_a_point_asks_and_runs_with_yes(cli: Any, project: Path, engine: FakeEngine) -> None:  # noqa: F811
    refused = cli("computer", "click", "--at", "140,115", cwd=project)  # no terminal to ask in, no --yes
    assert refused.code != 0 and engine.sent("click_at") == []
    clicked = cli("--yes", "--json", "computer", "click", "--at", "140,115", "--double", cwd=project)
    assert clicked.code == 0 and engine.sent("click_at") == [
        ("click_at", {"x": 140, "y": 115, "button": "left", "count": 2})
    ]
    assert clicked.json()["output"]["element"]["name"] == "Save"
    by_text = cli("--yes", "computer", "click", "--text", "Delete", "--right", cwd=project)
    assert by_text.code == 0 and engine.sent("click_at")[-1][1]["button"] == "right"
    nothing = cli("computer", "click", cwd=project)
    assert nothing.code != 0 and "--at X,Y" in nothing.stderr


def test_window_quit_menu_clipboard_and_drag(cli: Any, project: Path, engine: FakeEngine) -> None:  # noqa: F811
    assert cli("--yes", "computer", "window", "Notes", "--frame", "0,25,640,480", cwd=project).code == 0
    assert engine.windows[0]["width"] == 640
    assert cli("--yes", "computer", "menu", "Notes", "File > Save", cwd=project).code == 0
    assert engine.chosen == [["File", "Save"]]
    assert cli("--yes", "computer", "clipboard", "--set", "hi", cwd=project).code == 0 and engine.clipboard == "hi"
    read = cli("--yes", "--json", "computer", "clipboard", cwd=project)
    assert read.json()["output"]["text"] == "hi"
    assert cli("--yes", "computer", "drag", "1,2", "3,4", cwd=project).code == 0
    assert cli("--yes", "computer", "quit", "Notes", cwd=project).code == 0 and "Notes" not in engine.running
    assert cli("computer", "move", "5,6", cwd=project).code == 0 and engine.pointer == (5, 6)


def test_desktop_scroll_and_screenshot(cli: Any, project: Path, engine: FakeEngine) -> None:  # noqa: F811
    assert cli("computer", "scroll", "down", "--at", "5,6", cwd=project).code == 0
    assert engine.sent("scroll")[-1][1] == {"direction": "down", "amount": 3, "x": 5, "y": 6}
    shot = cli("--json", "computer", "screenshot", cwd=project)
    assert shot.code == 0 and Path(shot.json()["output"]["path"]).is_file()


def test_the_contract_is_printed_exactly(cli: Any, project: Path) -> None:
    printed = cli("computer", "protocol", cwd=project)
    assert printed.code == 0 and json.loads(printed.stdout) == json.loads(json.dumps(describe()))
