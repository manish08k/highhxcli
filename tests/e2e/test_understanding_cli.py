"""`highhx do` on HighhX Free with no AI: natural language → HXIR → plan → executor → verification,
or an explanation of what is unclear — and nothing runs then."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import copy_fixture
from tests.e2e._helpers import highhx
from tests.e2e.test_free_without_ai import NO_AI

pytestmark = pytest.mark.e2e


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = copy_fixture("python_project", tmp_path)
    assert highhx("init", "--yes", cwd=root, env=NO_AI).code == 0
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "architecture.md").write_text("# architecture\n")
    (root / "docs" / "plan.pdf").write_bytes(b"%PDF")
    (root / "reports").mkdir()
    (root / "reports" / "q3.pdf").write_bytes(b"%PDF")
    return root


def test_a_file_request_is_planned_as_hxir_and_runs_through_the_executor(project: Path) -> None:
    planned = highhx("do", "--plan", "--json", "find the architecture document", cwd=project, env=NO_AI)
    assert planned.code == 0, planned.stderr
    decision = planned.json()
    assert decision["route"] == "local" and decision["hxir"]["status"] == "resolved"
    assert decision["plan"]["steps"][0]["catalog_action"] == "filesystem.find"
    assert decision["hxir"]["entities"][0]["value"] == "docs/architecture.md"

    ran = highhx("do", "--json", "find my pdfs", cwd=project, env=NO_AI)
    assert ran.code == 0, ran.stderr
    result = ran.json()["result"]
    assert result["ok"] and result["verification"] == "verified"
    assert "docs/plan.pdf" in result["steps"][0]["summary"] and "reports/q3.pdf" in result["steps"][0]["summary"]


@pytest.mark.parametrize(
    ("request_text", "says"),
    [
        ("open it", "Nothing earlier in this conversation for 'it' to refer to"),
        ("open the pdf", "Which one do you mean?"),
        ("find the pdf I downloaded yesterday", "outside this project"),
        ("open the latest pdf but don't open anything", "contradicts itself"),
    ],
)
def test_unclear_requests_explain_and_run_nothing(project: Path, request_text: str, says: str) -> None:
    result = highhx("do", request_text, cwd=project, env=NO_AI)
    assert result.code == 1, (result.stdout, result.stderr)
    shown = result.stdout + result.stderr
    assert says in shown and "Nothing ran." in shown


def test_open_ended_requests_still_need_pro(project: Path) -> None:
    result = highhx("do", "run the tests and fix the failures", cwd=project, env=NO_AI)
    assert result.code == 2 and "highhx agent" in result.stderr
