"""Regression tests for problems found during the production audit."""

import io
import json
import sys
from pathlib import Path

import pytest

from highhx.config.loader import load_yaml
from highhx.core.errors import ConfigError
from highhx.project.detector import detect_project
from highhx.security.secrets import Redactor
from highhx.ui.output import Output
from highhx.ui.terminal import ASCII_SYMBOLS
from highhx.workflows.loader import WorkflowLoader
from highhx.workflows.validator import validate_file

GH = "ghp_" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2Y"


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "wf.yaml"
    path.write_text("name: a\nsteps:\n  - id: x\n    run: echo\n    run: rm -rf build\n")
    with pytest.raises(ConfigError) as info:
        load_yaml(path)
    assert "duplicate key" in info.value.message
    assert not validate_file(path, check_tools=False).ok


def test_boolean_conditions_and_json_workflows(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("name: a\nsteps:\n  - id: x\n    run: echo\n    if: false\n")
    (tmp_path / "b.json").write_text(json.dumps({"name": "b", "on": ["push"], "steps": [{"id": "y", "run": "echo"}]}))
    loader = WorkflowLoader.for_project(tmp_path)
    assert loader.keys() == ["a", "b"] and "b" in loader
    assert loader.load("a").steps[0].condition == "false"
    report = validate_file(tmp_path / "a.yaml", check_tools=False)
    assert report.ok and any("always false" in w for w in report.warnings)
    assert loader.load("b").triggers == ["push"]


def test_output_interpolation_into_commands_warns(tmp_path: Path) -> None:
    (tmp_path / "w.yaml").write_text(
        "name: w\nsteps:\n  - id: a\n    run: echo\n  - id: b\n    depends_on: [a]\n    run: echo ${{ steps.a.outputs.v }}\n"
    )
    report = validate_file(tmp_path / "w.yaml", check_tools=False)
    assert report.ok and any("shell injection" in w for w in report.warnings)


def test_malformed_manifests_are_reported(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{ not json")
    (tmp_path / "pyproject.toml").write_text("[project\nname=")
    profile = detect_project(tmp_path)
    assert {p.split(":")[0] for p in profile.problems} == {"package.json", "pyproject.toml"}


def test_mixed_project_prefers_python_and_keeps_both_stacks(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='api'\nversion='1.0.0'\ndependencies=['fastapi']\n")
    (tmp_path / "package.json").write_text(json.dumps({"name": "ui", "dependencies": {"react": "18"}}))
    (tmp_path / "Dockerfile").write_text("FROM python\n")
    profile = detect_project(tmp_path)
    assert profile.primary == "python"
    assert {"python", "react", "node", "docker"} <= set(profile.stacks)


@pytest.mark.parametrize(
    ("files", "primary", "command"),
    [
        ({"go.mod": "module example.com/svc\n"}, "go", ("test", "go test ./...")),
        ({"Cargo.toml": "[package]\nname='x'\nversion='0.1.0'\n"}, "rust", ("build", "cargo build")),
        ({"Makefile": "all:\n\tcc main.c\ntest:\n\t./t\n", "main.c": "int main(){}"}, "cpp", ("test", "make test")),
    ],
)
def test_go_rust_c_detection(tmp_path: Path, files: dict[str, str], primary: str, command: tuple[str, str]) -> None:
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    profile = detect_project(tmp_path)
    assert profile.primary == primary and profile.commands[command[0]] == command[1]


def test_output_redacts_everything_it_prints() -> None:
    buffer = io.StringIO()
    out = Output(stdout=buffer, stderr=buffer, json_mode=True, redactor=Redactor(["my-db-password-1"]))
    out.json({"stdout": f"pw=my-db-password-1 token={GH}"})
    assert "my-db-password-1" not in buffer.getvalue() and GH not in buffer.getvalue()
    json.loads(buffer.getvalue())


def test_ascii_symbols_are_not_rich_markup() -> None:
    buffer = io.StringIO()
    out = Output(stdout=buffer, stderr=buffer)
    out.symbols = ASCII_SYMBOLS
    out.success("done")
    assert buffer.getvalue().strip() == "OK done"


def test_exec_json_output_is_redacted(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    cli("env", "set", f"GITHUB_TOKEN={GH}", cwd=tmp_path)
    result = cli("exec", "--json", sys.executable, "-c", "import os; print(os.environ['GITHUB_TOKEN'])", cwd=tmp_path)
    assert GH not in result.stdout and "[REDACTED]" in result.json()["stdout"]


def test_corrupt_state_database_is_diagnosed_and_repaired(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    db = tmp_path / ".highhx" / "state" / "highhx.db"
    db.write_text("this is not sqlite")
    assert cli("status", cwd=tmp_path).code == 0
    problems = cli("diagnose", "--json", cwd=tmp_path).json()["problems"]
    assert any(p["id"] == "state-db-unreadable" for p in problems)
    assert cli("repair", "--yes", cwd=tmp_path).code == 0
    assert list(db.parent.glob("highhx.db.corrupt-*"))
    assert cli("exec", sys.executable, "-c", "pass", cwd=tmp_path).code == 0
    assert cli("history", "--json", cwd=tmp_path).json()["executions"]


def test_global_config_profile_option_and_env_profile_option_coexist(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    profiles = tmp_path / ".highhx" / "profiles"
    profiles.mkdir()
    (profiles / "ci.yaml").write_text("commands:\n  test: ci-test\n")
    assert (
        cli("config", "get", "commands.test", "--config-profile", "ci", "--json", cwd=tmp_path).json()["value"]
        == "ci-test"
    )
    assert cli("env", "check", "--profile", "staging", "--json", cwd=tmp_path).json()["profile"] == "staging"
    assert (
        cli("--config-profile", "ci", "env", "show", "--profile", "production", "--json", cwd=tmp_path).json()[
            "profile"
        ]
        == "production"
    )


def test_debug_flag_enables_logging(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import logging

    cli("init", cwd=tmp_path)
    cli("status", "--debug", cwd=tmp_path)
    assert logging.getLogger("highhx").handlers
    assert (tmp_path / ".highhx" / "logs" / "highhx-debug.log").exists()
