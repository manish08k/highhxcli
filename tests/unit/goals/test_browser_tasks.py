"""The goal loop on the HighhX browser itself (ChromeBrowser over a scripted Chrome): tabs that are
already open, closed under the task, new tabs, lost connections and failed navigations — and the
``highhx computer task`` command end to end."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from highhx.computer.browser import ChromeBrowser
from highhx.computer.session import ComputerSession
from highhx.goals.state import COMPLETED, FAILED
from tests.unit.computer.test_browser_recovery import FakeChrome, _sent
from tests.unit.goals.conftest import Kit, make_kit
from tests.unit.goals.test_loop import act, task


@pytest.fixture
def chrome_kit(app: Any, browser: ChromeBrowser) -> Kit:
    return make_kit(app, browser)


def _pages(chrome: FakeChrome) -> list[str]:
    return sorted(str(t["url"]) for t in chrome.tabs.values())


def test_navigation_in_the_real_browser_runtime(chrome_kit: Kit, chrome: FakeChrome) -> None:
    state = chrome_kit.run(
        task(
            act("navigate", value="https://unknown-site.test/"),
            success_conditions=[{"url_contains": "unknown-site.test"}],
        )
    )
    assert state.status == COMPLETED and _pages(chrome) == ["https://unknown-site.test/"]


def test_an_already_open_page_is_reused_not_duplicated(chrome_kit: Kit, chrome: FakeChrome) -> None:
    goal = task(act("navigate", value="https://a.test/"))
    chrome_kit.run(goal)
    navigations = len(_sent(chrome, "Page.navigate"))
    state = chrome_kit.run(goal)
    assert state.status == COMPLETED and len(chrome.tabs) == 1
    assert len(_sent(chrome, "Page.navigate")) == navigations  # not reloaded


def test_another_site_opens_beside_the_current_page(chrome_kit: Kit, chrome: FakeChrome) -> None:
    state = chrome_kit.run(task(act("navigate", value="https://a.test/"), act("navigate", value="https://b.test/")))
    assert state.status == COMPLETED and _pages(chrome) == ["https://a.test/", "https://b.test/"]


def test_the_same_site_in_a_new_tab(chrome_kit: Kit, chrome: FakeChrome, browser: ChromeBrowser) -> None:
    state = chrome_kit.run(task(act("navigate", value="https://a.test/"), act("new_tab", value="https://a.test/")))
    assert state.status == COMPLETED and _pages(chrome) == ["https://a.test/", "https://a.test/"]
    assert browser._target_id != "t1"  # working in the new one


def test_a_tab_closed_during_the_task_is_recreated(chrome_kit: Kit, chrome: FakeChrome) -> None:
    chrome_kit.run(task(act("navigate", value="https://a.test/")))
    chrome.close_tab("t1")  # the person closes it between steps
    state = chrome_kit.run(task(act("navigate", value="https://a.test/next")))
    assert state.status == COMPLETED and _pages(chrome) == ["https://a.test/next"]
    assert any(e.kind == "RECOVERY" and "tab was closed" in e.message for e in chrome_kit.log.entries)


def test_a_lost_connection_reconnects_and_continues(
    chrome_kit: Kit, chrome: FakeChrome, browser: ChromeBrowser
) -> None:
    chrome_kit.run(task(act("navigate", value="https://a.test/")))
    assert browser._conn is not None
    browser._conn.ws.close()
    state = chrome_kit.run(task(act("navigate", value="https://b.test/"), act("back")))
    assert state.status == COMPLETED and browser.reconnects == 1
    assert any(e.kind == "RECOVERY" and "reconnected" in e.message for e in chrome_kit.log.entries)
    assert state.recoveries >= 1


def test_a_navigation_error_is_reported_not_retried_forever(chrome_kit: Kit, chrome: FakeChrome) -> None:
    chrome.nav_error = "net::ERR_NAME_NOT_RESOLVED"
    state = chrome_kit.run(task(act("navigate", value="https://no-such-host.test/")))
    assert state.status == FAILED and "ERR_NAME_NOT_RESOLVED" in state.reason
    assert len(_sent(chrome, "Page.navigate")) <= 3


# ----------------------------------------------------------------- the CLI
@pytest.fixture
def live(browser: ChromeBrowser, monkeypatch: pytest.MonkeyPatch) -> ChromeBrowser:
    monkeypatch.setattr(ComputerSession, "browser", property(lambda self: browser))
    return browser


def test_cli_runs_a_deterministic_task(
    cli: Callable[..., Any], tmp_path: Path, live: ChromeBrowser, chrome: FakeChrome
) -> None:
    result = cli("computer", "task", "open example.org", cwd=tmp_path)
    assert result.code == 0, result.stdout + result.stderr
    for kind in ("TASK:", "PLAN:", "OBSERVE:", "ACTION:", "RESULT:", "VERIFY:", "FINAL:"):
        assert kind in result.stdout
    assert ("Task completed" in result.stdout and _pages(chrome) == ["https://example.org/"]) or _pages(chrome) == [
        "https://example.org"
    ]


def test_cli_json_output_carries_the_state_and_log(
    cli: Callable[..., Any], tmp_path: Path, live: ChromeBrowser
) -> None:
    result = cli("--json", "computer", "task", "open example.org", cwd=tmp_path)
    data = json.loads(result.stdout)
    assert data["ok"] and data["status"] == "completed" and data["steps"][0]["action"]["action"] == "browser.navigate"
    assert data["log"][0]["kind"] == "TASK" and Path(data["log_file"]).is_file()


def test_cli_runs_task_ir_from_a_file(cli: Callable[..., Any], tmp_path: Path, live: ChromeBrowser) -> None:
    ir = tmp_path / "task.json"
    ir.write_text(json.dumps({"goal": "open a page", "steps": [act("navigate", value="https://a.test/")]}))
    assert cli("computer", "task", "--ir", str(ir), cwd=tmp_path).code == 0


def test_cli_rejects_malformed_ir(
    cli: Callable[..., Any], tmp_path: Path, live: ChromeBrowser, chrome: FakeChrome
) -> None:
    ir = tmp_path / "task.json"
    ir.write_text(json.dumps({"goal": "x", "steps": [{"action": "shell.run", "value": "rm -rf ~", "reason": "x"}]}))
    result = cli("computer", "task", "--ir", str(ir), cwd=tmp_path)
    assert result.code == 8 and "is not one of" in result.stdout + result.stderr
    assert chrome.sent == []  # nothing ran


def test_cli_dry_run_shows_the_ir_without_running(
    cli: Callable[..., Any], tmp_path: Path, live: ChromeBrowser, chrome: FakeChrome
) -> None:
    result = cli("--dry-run", "computer", "task", "open youtube and play lofi", cwd=tmp_path)
    assert result.code == 0 and "browser.click" in result.stdout and "media_playing" in result.stdout
    assert chrome.sent == []


def test_cli_free_new_task_needs_pro(
    cli: Callable[..., Any], tmp_path: Path, live: ChromeBrowser, chrome: FakeChrome
) -> None:
    result = cli("computer", "task", "open LeetCode and solve one problem", cwd=tmp_path)
    assert result.code == 10 and "HighhX Pro" in result.stdout + result.stderr
    assert chrome.sent == []


def test_cli_non_browser_requests_are_redirected(cli: Callable[..., Any], tmp_path: Path, live: ChromeBrowser) -> None:
    result = cli("computer", "task", "run the tests", cwd=tmp_path)
    assert result.code == 2 and "not browser work" in result.stdout + result.stderr


def test_cli_schema(cli: Callable[..., Any], tmp_path: Path) -> None:
    data = json.loads(cli("--json", "computer", "task", "--schema", cwd=tmp_path).stdout)
    assert set(data) == {"task", "action", "condition"}
