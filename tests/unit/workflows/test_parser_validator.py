from pathlib import Path

import pytest
import yaml

from highhx.core.errors import WorkflowError
from highhx.workflows.loader import WorkflowLoader
from highhx.workflows.parser import parse_workflow
from highhx.workflows.validator import validate_data, validate_file


def validate(doc: dict, loader: WorkflowLoader | None = None):  # type: ignore[no-untyped-def]
    return validate_data(doc, name="wf", loader=loader, check_tools=False)


def test_parses_full_workflow() -> None:
    spec = parse_workflow(
        {
            "name": "production",
            "settings": {"fail_fast": True, "max_parallel": 4, "timeout": "10m"},
            "steps": [
                {"id": "test", "run": "pytest"},
                {"id": "lint", "run": "ruff check ."},
                {"id": "build", "run": "docker build -t app .", "depends_on": ["test", "lint"]},
                {
                    "id": "deploy",
                    "run": "./deploy.sh",
                    "depends_on": "build",
                    "approval": True,
                    "retry": {"attempts": 3, "delay": 10},
                },
            ],
        }
    )
    deploy = spec.step("deploy")
    assert deploy.depends_on == ["build"] and deploy.approval is not None
    assert deploy.retry.attempts == 3 and deploy.retry.delay == 10
    assert spec.settings.timeout == 600


def test_schema_errors_are_reported_together() -> None:
    with pytest.raises(WorkflowError) as info:
        parse_workflow({"name": "x", "steps": [{"id": "a", "run": "x", "bogus": 1}, {"id": "b"}], "extra": True})
    details = "\n".join(info.value.details)
    assert "bogus: unknown field" in details
    assert "exactly one of 'run', 'uses', 'action', 'choose', 'wait', 'handoff' or 'set'" in details
    assert "extra: unknown field" in details


def test_duplicate_ids() -> None:
    report = validate({"name": "x", "steps": [{"id": "a", "run": "echo"}, {"id": "a", "run": "echo"}]})
    assert any("duplicate step id 'a'" in e for e in report.errors)


def test_missing_and_circular_dependencies() -> None:
    missing = validate(
        {"name": "x", "steps": [{"id": "a", "run": "echo", "depends_on": ["bulid"]}, {"id": "build", "run": "echo"}]}
    )
    assert any("unknown step 'bulid' (did you mean 'build'?)" in e for e in missing.errors)
    circular = validate(
        {
            "name": "x",
            "steps": [{"id": "a", "run": "echo", "depends_on": ["b"]}, {"id": "b", "run": "echo", "depends_on": ["a"]}],
        }
    )
    assert any("circular dependency" in e for e in circular.errors)


def test_invalid_commands_and_expressions() -> None:
    report = validate(
        {
            "name": "x",
            "steps": [
                {"id": "a", "run": "echo 'unterminated"},
                {"id": "b", "run": "echo ${{ steps.c.outputs.x }}"},
                {"id": "c", "run": "echo", "if": "env.X =="},
                {"id": "d", "run": "echo ${{ bogus.value }}"},
            ],
        }
    )
    text = "\n".join(report.errors)
    assert "cannot parse command" in text
    assert "not a dependency of 'b'" in text
    assert "invalid expression" in text
    assert "unknown context 'bogus'" in text


def test_impossible_workflow() -> None:
    report = validate(
        {
            "name": "x",
            "steps": [{"id": "a", "run": "echo", "if": "false"}, {"id": "b", "run": "echo", "depends_on": ["a"]}],
        }
    )
    assert any("can never run" in e for e in report.errors)


def test_dangerous_command_without_approval_warns() -> None:
    report = validate({"name": "x", "steps": [{"id": "push", "run": "git push origin main"}]})
    assert report.ok
    assert any("approval" in w for w in report.warnings)


def test_reusable_workflow_checks(tmp_path: Path) -> None:
    (tmp_path / "child.yaml").write_text(
        yaml.safe_dump({"name": "child", "inputs": {"env": {"required": True}}, "steps": [{"id": "s", "run": "echo"}]})
    )
    (tmp_path / "loop.yaml").write_text(yaml.safe_dump({"name": "loop", "steps": [{"id": "s", "uses": "loop"}]}))
    loader = WorkflowLoader.for_project(tmp_path)
    report = validate({"name": "parent", "steps": [{"id": "c", "uses": "child", "with": {"nope": 1}}]}, loader)
    text = "\n".join(report.errors)
    assert "missing required input 'env'" in text and "has no input 'nope'" in text
    recursive = validate_file(tmp_path / "loop.yaml", loader=loader, check_tools=False)
    assert any("Recursive workflow reference" in e for e in recursive.errors)


def test_invalid_yaml_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("name: [unclosed\n")
    assert not validate_file(path).ok


def test_unquoted_on_key_is_normalized(tmp_path: Path) -> None:
    (tmp_path / "t.yaml").write_text("name: t\non: [push]\nsteps:\n  - id: a\n    run: echo\n")
    loader = WorkflowLoader.for_project(tmp_path)
    assert loader.list()[0].triggers == ("push",)
    assert loader.load("t").triggers == ["push"]
    assert validate_file(tmp_path / "t.yaml", check_tools=False).ok
