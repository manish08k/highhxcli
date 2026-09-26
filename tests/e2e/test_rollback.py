from pathlib import Path

import pytest

from tests.e2e._helpers import highhx
from tests.e2e.test_deploy import configure_target

pytestmark = pytest.mark.e2e


def test_rollback_restores_previous_version(git_python_project: Path) -> None:
    highhx("init", cwd=git_python_project)
    marker = configure_target(git_python_project)
    assert highhx("rollback", "--yes", cwd=git_python_project).code == 4
    for version in ("1.0.0", "1.1.0"):
        assert highhx("deploy", "--version", version, "--yes", cwd=git_python_project).code == 0
    assert marker.read_text() == "1.1.0"
    result = highhx("rollback", "--yes", "--json", cwd=git_python_project)
    assert result.code == 0, result.stderr
    assert result.json()["deployment"]["version"] == "1.0.0" and marker.read_text() == "1.0.0"
    history = highhx("deploy", "status", "--history", "--json", cwd=git_python_project).json()["deployments"]
    assert [d["status"] for d in history] == ["succeeded", "rolled_back", "succeeded"]
