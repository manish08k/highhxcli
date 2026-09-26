import json
import threading
import time
from pathlib import Path

import pytest
import yaml

from highhx.core.errors import ApprovalDeniedError, WorkflowError
from highhx.core.result import Status
from highhx.workflows.engine import WorkflowEngine, parse_outputs_file
from highhx.workflows.loader import WorkflowLoader
from tests.conftest import PY


def q(code: str) -> str:
    """A shell-free command string running Python code."""
    return f'"{PY}" -c "{code}"'


def stamp(name: str, sleep: float = 0.0) -> str:
    code = (
        "import json, os, time; "
        f"s = time.time(); time.sleep({sleep}); "
        f"open(os.path.join(os.environ['STAMPS'], '{name}.json'), 'w').write(json.dumps([s, time.time()]))"
    )
    return q(code)


@pytest.fixture
def setup(tmp_path: Path, make_engine):  # type: ignore[no-untyped-def]
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    stamps = tmp_path / "stamps"
    stamps.mkdir()

    def _make(doc: dict, **engine_kwargs):  # type: ignore[no-untyped-def]
        (wf_dir / f"{doc['name']}.yaml").write_text(yaml.safe_dump(doc))
        kit = make_engine(**engine_kwargs)
        kit.engine.ctx.env["STAMPS"] = str(stamps)
        return WorkflowEngine(kit.engine, WorkflowLoader.for_project(wf_dir), root=tmp_path), kit

    def _stamp(name: str) -> tuple[float, float]:
        return tuple(json.loads((stamps / f"{name}.json").read_text()))  # type: ignore[return-value]

    return _make, _stamp, stamps


def test_dependencies_complete_before_dependents_and_parallelism(setup) -> None:  # type: ignore[no-untyped-def]
    make, read, _ = setup
    engine, _ = make(
        {
            "name": "ci",
            "settings": {"max_parallel": 4},
            "steps": [
                {"id": "test", "run": stamp("test", 0.6)},
                {"id": "lint", "run": stamp("lint", 0.6)},
                {"id": "build", "run": stamp("build"), "depends_on": ["test", "lint"]},
            ],
        }
    )
    started = time.monotonic()
    result = engine.run("ci")
    assert result.status == Status.SUCCESS
    test, lint, build = read("test"), read("lint"), read("build")
    assert build[0] >= test[1] and build[0] >= lint[1]
    assert lint[0] < test[1] and test[0] < lint[1], "independent steps should overlap"
    assert time.monotonic() - started < 1.15


def test_max_parallel_one_serializes(setup) -> None:  # type: ignore[no-untyped-def]
    make, read, _ = setup
    engine, _ = make(
        {
            "name": "serial",
            "settings": {"max_parallel": 1},
            "steps": [{"id": "a", "run": stamp("a", 0.2)}, {"id": "b", "run": stamp("b", 0.2)}],
        }
    )
    engine.run("serial")
    a, b = read("a"), read("b")
    assert b[0] >= a[1] or a[0] >= b[1]


def test_fail_fast_skips_dependents_and_cancels_running(setup) -> None:  # type: ignore[no-untyped-def]
    make, _, stamps = setup
    engine, kit = make(
        {
            "name": "ff",
            "steps": [
                {"id": "bad", "run": q("import sys; sys.exit(3)")},
                {"id": "slow", "run": q("import time; time.sleep(20)")},
                {"id": "after", "run": stamp("after"), "depends_on": ["bad"]},
                {"id": "cleanup", "run": stamp("cleanup"), "depends_on": ["bad"], "if": "failure()"},
            ],
        }
    )
    started = time.monotonic()
    result = engine.run("ff")
    assert result.status == Status.FAILED
    assert result.steps["bad"].exit_code == 3
    assert result.steps["slow"].status == Status.CANCELLED
    assert result.steps["after"].status == Status.SKIPPED
    assert result.steps["cleanup"].status == Status.SUCCESS
    assert not (stamps / "after.json").exists()
    assert time.monotonic() - started < 10
    record = kit.history.get(result.execution_id)
    assert record.status == "failed" and {s.step_id for s in record.steps} == {"bad", "slow", "after", "cleanup"}


def test_continue_on_error_and_no_fail_fast(setup) -> None:  # type: ignore[no-untyped-def]
    make, _, _ = setup
    engine, _ = make(
        {
            "name": "soft",
            "settings": {"fail_fast": False},
            "steps": [
                {"id": "flaky", "run": q("import sys; sys.exit(1)"), "continue_on_error": True},
                {"id": "next", "run": q("pass"), "depends_on": ["flaky"]},
                {"id": "broken", "run": q("import sys; sys.exit(1)")},
                {"id": "independent", "run": q("pass")},
            ],
        }
    )
    result = engine.run("soft")
    assert result.steps["flaky"].allowed_failure
    assert result.steps["next"].status == Status.SUCCESS
    assert result.steps["independent"].status == Status.SUCCESS
    assert result.status == Status.FAILED


def test_retries_and_timeouts(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    make, _, _ = setup
    counter = tmp_path / "count"
    code = f"import pathlib,sys; p=pathlib.Path(r'{counter}'); n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); sys.exit(0 if n>=2 else 1)"
    engine, _ = make(
        {
            "name": "rt",
            "settings": {"fail_fast": False},
            "steps": [
                {"id": "flaky", "run": q(code), "retry": {"attempts": 3, "delay": 0.05}},
                {"id": "slow", "run": q("import time; time.sleep(10)"), "timeout": "0.5s"},
            ],
        }
    )
    result = engine.run("rt")
    assert result.steps["flaky"].status == Status.SUCCESS
    assert result.steps["flaky"].commands[0].attempts == 3
    assert result.steps["slow"].status == Status.TIMEOUT


def test_workflow_timeout(setup) -> None:  # type: ignore[no-untyped-def]
    make, _, _ = setup
    engine, _ = make(
        {"name": "wt", "settings": {"timeout": "0.5s"}, "steps": [{"id": "s", "run": q("import time; time.sleep(10)")}]}
    )
    result = engine.run("wt")
    assert result.status == Status.TIMEOUT


def test_outputs_variables_and_conditions(setup) -> None:  # type: ignore[no-untyped-def]
    make, _, _ = setup
    write = q("import os; open(os.environ['HIGHHX_OUTPUT'], 'a').write('version=2.0.1\\n')")
    engine, _ = make(
        {
            "name": "vars",
            "inputs": {"target": {"default": "staging"}},
            "vars": {"app": "demo"},
            "steps": [
                {"id": "build", "run": write},
                {
                    "id": "use",
                    "depends_on": ["build"],
                    "run": q("import os; assert os.environ['V'] == '2.0.1-demo-staging'"),
                    "env": {"V": "${{ steps.build.outputs.version }}-${{ vars.app }}-${{ inputs.target }}"},
                },
                {
                    "id": "skipped",
                    "depends_on": ["build"],
                    "if": "inputs.target == 'production'",
                    "run": q("raise SystemExit(1)"),
                },
            ],
            "outputs": {"version": "${{ steps.build.outputs.version }}"},
        }
    )
    result = engine.run("vars")
    assert result.status == Status.SUCCESS
    assert result.outputs == {"version": "2.0.1"}
    assert result.steps["skipped"].status == Status.SKIPPED


def test_approval_step_denied_without_terminal(setup) -> None:  # type: ignore[no-untyped-def]
    make, _, stamps = setup
    engine, _ = make(
        {"name": "ap", "steps": [{"id": "deploy", "run": stamp("deploy"), "approval": True}]}, interactive=False
    )
    result = engine.run("ap")
    assert result.steps["deploy"].status == Status.FAILED
    assert "Not approved" in result.steps["deploy"].message
    assert not (stamps / "deploy.json").exists()


def test_approval_step_with_yes(setup) -> None:  # type: ignore[no-untyped-def]
    make, _, stamps = setup
    engine, _ = make(
        {"name": "ap2", "steps": [{"id": "deploy", "run": stamp("deploy"), "approval": {"risk": "critical"}}]},
        interactive=False,
        yes=True,
    )
    assert engine.run("ap2").ok
    assert (stamps / "deploy.json").exists()


def test_dry_run_executes_nothing(setup) -> None:  # type: ignore[no-untyped-def]
    make, _, stamps = setup
    engine, _ = make({"name": "dr", "steps": [{"id": "a", "run": stamp("a")}]}, dry_run=True)
    result = engine.run("dr")
    assert result.dry_run and result.steps["a"].status == Status.SKIPPED
    assert not (stamps / "a.json").exists()


def test_reusable_workflow_and_inputs(setup) -> None:  # type: ignore[no-untyped-def]
    make, _, _ = setup
    make(
        {
            "name": "child",
            "inputs": {"word": {"required": True}},
            "steps": [
                {
                    "id": "echo",
                    "run": q(
                        "import os; open(os.environ['HIGHHX_OUTPUT'],'w').write('said=' + os.environ['W'] + '\\n')"
                    ),
                    "env": {"W": "${{ inputs.word }}"},
                }
            ],
            "outputs": {"said": "${{ steps.echo.outputs.said }}"},
        }
    )
    engine, _ = make(
        {
            "name": "parent",
            "steps": [{"id": "call", "uses": "child", "with": {"word": "hi"}}],
            "outputs": {"got": "${{ steps.call.outputs.said }}"},
        }
    )
    result = engine.run("parent")
    assert result.ok and result.outputs["got"] == "hi"


def test_invalid_workflow_is_rejected_before_running(setup) -> None:  # type: ignore[no-untyped-def]
    make, _, stamps = setup
    engine, _ = make({"name": "bad", "steps": [{"id": "a", "run": stamp("a"), "depends_on": ["missing"]}]})
    with pytest.raises(WorkflowError):
        engine.run("bad")
    assert not (stamps / "a.json").exists()


def test_cancellation_stops_workflow(setup) -> None:  # type: ignore[no-untyped-def]
    make, _, _ = setup
    engine, kit = make(
        {
            "name": "cx",
            "steps": [
                {"id": "long", "run": q("import time; time.sleep(20)")},
                {"id": "after", "run": q("pass"), "depends_on": ["long"]},
            ],
        }
    )
    threading.Timer(0.5, kit.engine.ctx.cancel.cancel, args=("test",)).start()
    started = time.monotonic()
    result = engine.run("cx")
    assert result.steps["long"].status == Status.CANCELLED
    assert result.steps["after"].status in (Status.CANCELLED, Status.SKIPPED)
    assert result.status == Status.CANCELLED
    assert time.monotonic() - started < 10


def test_missing_required_input(setup) -> None:  # type: ignore[no-untyped-def]
    make, _, _ = setup
    engine, _ = make({"name": "in", "inputs": {"target": {"required": True}}, "steps": [{"id": "a", "run": q("pass")}]})
    from highhx.core.errors import ValidationError

    with pytest.raises(ValidationError):
        engine.run("in")


def test_parse_outputs_file(tmp_path: Path) -> None:
    path = tmp_path / "out"
    path.write_text("a=1\nnotes<<EOF\nline1\nline2\nEOF\nb=x=y\n")
    assert parse_outputs_file(path) == {"a": "1", "notes": "line1\nline2", "b": "x=y"}


def test_approval_denied_error_type(make_engine) -> None:  # type: ignore[no-untyped-def]
    from highhx.approvals.risk import RiskLevel

    kit = make_engine(interactive=False)
    with pytest.raises(ApprovalDeniedError):
        kit.engine.approve("x", RiskLevel.DANGEROUS)
