"""filesystem.find through the action executor: confined, metadata only, secrets skipped, verified."""

from __future__ import annotations

from pathlib import Path

import pytest

from highhx.core.errors import ValidationError
from highhx.plans.planner import describe_action
from highhx.verification.strategies import Evidence, verify


def test_find_is_safe_confined_and_verified(agent_project: Path, executor_for) -> None:
    (agent_project / "docs").mkdir()
    (agent_project / "docs" / "architecture.md").write_text("# architecture\n")
    (agent_project / "docs" / "plan.pdf").write_bytes(b"%PDF")
    (agent_project / "secrets.yaml").write_text("token: x\n")  # secret files are never listed
    (agent_project / "deploy.key").write_text("-----BEGIN-----\n")
    executor, ui = executor_for(agent_project)

    planned = executor.plan("filesystem.find", {"kinds": ["document"], "keywords": ["architecture"]})
    assert planned.decision.risk.label == "safe" and not planned.decision.asks
    result = executor.execute(planned)
    assert result.ok and [f["path"] for f in result.output["files"]] == ["docs/architecture.md"]
    assert set(result.output["files"][0]) == {"path", "name", "kind", "size", "modified"}  # never contents
    assert ui.requests == []  # nothing was put to the person for approval

    pdfs = executor.run("filesystem.find", {"kinds": ["pdf"]})
    assert [f["path"] for f in pdfs.output["files"]] == ["docs/plan.pdf"]
    for word in ("secrets", "deploy"):
        assert executor.run("filesystem.find", {"keywords": [word]}).output["files"] == []
    assert not executor.run("filesystem.find", {"path": "../"}).ok  # outside the project
    with pytest.raises(ValidationError):  # a closed input schema: never runs
        executor.run("filesystem.find", {"kinds": ["executable"]})
    bad_date = executor.run("filesystem.find", {"modified_after": "yesterday"})
    assert not bad_date.ok and "ISO" in bad_date.error

    info = describe_action("filesystem.find")
    assert (info.executor, info.verification) == ("filesystem", "files_found")
    evidence = Evidence("filesystem.find", {}, True, None, result.output, "", agent_project)
    assert verify("files_found", evidence).status == "verified"
    (agent_project / "docs" / "architecture.md").unlink()
    assert verify("files_found", evidence).failed
