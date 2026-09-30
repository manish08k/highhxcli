"""Checked observation (Cua's ``verify_state``): bounded predicates about one exact window,
satisfied / unsatisfied / unknown — unknown is never success — sampled until they hold stably."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from highhx.automation.engine.bridge import AutomationBridge
from highhx.computer.driver import HighhXDriver
from highhx.core.errors import UsageError
from tests.computer_use.environment import SimulatedDesktop
from tests.unit.automation.fakes import FakeEngine, engine  # noqa: F401

NOTES = 11  # the Notes window in the simulated desktop


def desktop() -> tuple[SimulatedDesktop, HighhXDriver]:
    env = SimulatedDesktop(
        {
            "front": "Notes",
            "apps": {
                "Notes": {
                    "window": {
                        "id": NOTES,
                        "pid": 110,
                        "app": "Notes",
                        "title": "Doc",
                        "x": 0,
                        "y": 25,
                        "width": 800,
                        "height": 600,
                    },
                    "elements": [
                        {"role": "textbox", "name": "Title", "value": "Plan", "bounds": [40, 80, 400, 24]},
                        {"role": "button", "name": "Save", "bounds": [460, 80, 90, 24]},
                        {"role": "checkbox", "name": "Pinned", "bounds": [560, 80, 80, 24]},
                    ],
                }
            },
        }
    )
    return env, HighhXDriver(AutomationBridge(env))


def statuses(check: Any) -> list[str]:
    return [p.status for p in check.predicates]


def test_window_bounds_within_tolerance() -> None:
    _env, driver = desktop()
    frame = {"x": 2, "y": 25, "width": 800, "height": 603}
    assert driver.verify_state(NOTES, [{"window": {"bounds": frame}}], timeout_ms=0).ok
    moved = {"x": 100, "y": 25, "width": 800, "height": 600}
    check = driver.verify_state(NOTES, [{"window": {"bounds": moved}}], timeout_ms=0)
    assert check.status == "unsatisfied" and "[0, 25, 800, 600]" in check.predicates[0].detail


def test_a_closed_window() -> None:
    env, driver = desktop()
    env.apps["Notes"]["running"] = False
    assert driver.verify_state(NOTES, [{"window": {"exists": False}}], timeout_ms=0).ok
    check = driver.verify_state(
        NOTES, [{"window": {"exists": True}}, {"element": {"selector": {"label_contains": "Save"}}}], timeout_ms=0
    )
    assert statuses(check) == ["unsatisfied", "unknown"] and check.status == "unsatisfied"


def test_element_existence_value_and_enabled() -> None:
    _env, driver = desktop()
    expect = [
        {"element": {"selector": {"role": "button", "label_contains": "save"}, "exists": True, "enabled": True}},
        {"element": {"selector": {"role": "textbox", "label_contains": "Title"}, "value_equals": "Plan"}},
    ]
    check = driver.verify_state(NOTES, expect, timeout_ms=0)
    assert check.ok and statuses(check) == ["satisfied", "satisfied"] and check.window["id"] == NOTES
    wrong = driver.verify_state(
        NOTES, [{"element": {"selector": {"label_contains": "Title"}, "value_equals": "Other"}}], timeout_ms=0
    )
    assert wrong.status == "unsatisfied" and "'Plan'" in wrong.predicates[0].detail


def test_what_cannot_be_observed_is_unknown_never_success() -> None:
    _env, driver = desktop()
    missing = driver.verify_state(NOTES, [{"element": {"selector": {"label_contains": "Export"}}}], timeout_ms=0)
    assert missing.status == "unknown" and not missing.ok  # a bounded walk cannot prove absence
    selected = driver.verify_state(
        NOTES, [{"element": {"selector": {"label_contains": "Pinned"}, "selected": True}}], timeout_ms=0
    )
    assert selected.status == "unknown" and "not reported" in selected.predicates[0].detail


def test_it_waits_for_a_stable_state() -> None:
    env, driver = desktop()
    wanted = {"x": 0, "y": 25, "width": 640, "height": 480}
    ticks: list[float] = []

    def sleep(seconds: float) -> None:  # the application settles after the first sample
        ticks.append(seconds)
        env.apps["Notes"]["window"].update(width=640, height=480)

    from highhx.computer.verify import verify_state

    check = verify_state(driver, NOTES, [{"window": {"bounds": wanted}}], stable_samples=2, sleep=sleep)
    assert check.ok and check.samples == 3 and len(ticks) == 2


def test_a_state_that_never_holds_stops_at_the_timeout() -> None:
    _env, driver = desktop()
    from highhx.computer.verify import verify_state

    moved = {"x": 500, "y": 25, "width": 800, "height": 600}
    check = verify_state(driver, NOTES, [{"window": {"bounds": moved}}], timeout_ms=1000, sleep=lambda _s: None)
    assert check.status == "unsatisfied" and check.samples == 6  # 1 + 1000 ms / 200 ms, even with no real time


@pytest.mark.parametrize(
    "expect",
    [
        [],
        [{"element": {"selector": {"label_contains": "Save"}, "exists": False}}],
        [{"window": {"exists": True}, "element": {"selector": {"role": "button"}}}],
        [{"element": {"selector": {}}}],
        [{"window": {"exists": True}}] * 9,
        [{"window": {"size": 1}}],
    ],
)
def test_invalid_predicates_are_refused(expect: list[Any]) -> None:
    _env, driver = desktop()
    with pytest.raises(UsageError):
        driver.verify_state(NOTES, expect, timeout_ms=0)


def test_the_cli_exits_zero_only_when_satisfied(cli: Any, tmp_path: Path, engine: FakeEngine) -> None:  # noqa: F811
    ok = cli(
        "--json",
        "computer",
        "verify",
        "Notes",
        "--element",
        "Save",
        "--role",
        "button",
        "--timeout-ms",
        "0",
        cwd=tmp_path,
    )
    assert ok.code == 0 and ok.json()["output"]["status"] == "satisfied"
    moved = cli("computer", "verify", "--id", "7", "--frame", "5,5,5,5", "--timeout-ms", "0", cwd=tmp_path)
    assert moved.code != 0
    unknown = cli("computer", "verify", "Notes", "--element", "Export", "--timeout-ms", "0", cwd=tmp_path)
    assert unknown.code != 0
    bad = cli("computer", "verify", "Notes", "--expect", "{not json", cwd=tmp_path)
    assert bad.code != 0 and "JSON" in bad.stderr
