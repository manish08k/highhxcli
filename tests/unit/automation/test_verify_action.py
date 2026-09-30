"""computer.verify through the action executor: a read-only checked observation whose result
is the predicates' verdict (unknown or unsatisfied is not success)."""

from __future__ import annotations

from pathlib import Path

import pytest

from highhx.core.errors import ValidationError
from tests.unit.automation.fakes import FakeEngine


def test_the_action_reports_each_predicate(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    executor, _ = executor_for(agent_project)
    result = executor.run(
        "computer.verify",
        {
            "app": "Notes",
            "expect": [{"element": {"selector": {"role": "button", "label_contains": "Save"}}}],
            "timeout_ms": 0,
        },
    )
    assert result.ok and result.verified and result.output["status"] == "satisfied"
    failed = executor.run(
        "computer.verify",
        {"window": 7, "expect": [{"window": {"bounds": {"x": 9, "y": 9, "width": 9, "height": 9}}}], "timeout_ms": 0},
    )
    assert not failed.ok and failed.verified is False and "#0 unsatisfied" in failed.error
    with pytest.raises(ValidationError):  # refused before anything is observed
        executor.run("computer.verify", {"window": 7, "expect": [{"nope": 1}]})
