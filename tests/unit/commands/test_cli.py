import json
from pathlib import Path

import pytest

from highhx import __version__
from highhx.cli import SECTIONS
from highhx.cli import cli as root


def test_help_lists_sections(cli) -> None:  # type: ignore[no-untyped-def]
    result = cli("--help")
    assert result.code == 0
    for title, _ in SECTIONS:
        assert title in result.stdout


def test_every_section_command_is_registered() -> None:
    for _, names in SECTIONS:
        for name in names:
            assert name in root.commands, name


def test_version(cli) -> None:  # type: ignore[no-untyped-def]
    for flag in ("--version", "-V"):
        result = cli(flag)
        assert result.code == 0 and result.stdout.strip() == f"highhx {__version__}"


def test_no_arguments_shows_help(cli) -> None:  # type: ignore[no-untyped-def]
    result = cli()
    assert result.code == 0 and "Usage:" in result.stdout


def test_unknown_command_is_usage_error(cli) -> None:  # type: ignore[no-untyped-def]
    result = cli("definitely-not-a-command")
    assert result.code == 2 and "No such command" in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "deps",
        "env",
        "git",
        "db",
        "workflow",
        "plugin",
        "config",
        "security",
        "deploy",
        "docker",
        "schedule",
        "hook",
        "policy",
        "workspace",
        "profile",
    ],
)
def test_group_help(cli, command: str) -> None:  # type: ignore[no-untyped-def]
    result = cli(command, "--help")
    assert result.code == 0 and "Usage:" in result.stdout


def test_global_options_work_after_subcommand(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    result = cli("info", "--json", cwd=python_project)
    assert result.code == 0
    data = json.loads(result.stdout)
    assert data["profile"]["name"] == "pyapp"


def test_cwd_option(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    result = cli("--cwd", str(python_project), "info", "--json")
    assert json.loads(result.stdout)["profile"]["primary"] == "python"


def test_json_errors_are_machine_readable(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    result = cli("run", "missing", "--json", cwd=tmp_path)
    assert result.code == 4
    data = result.json()
    assert data["ok"] is False and data["error"] == "not_found"


def test_commands_requiring_project(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    result = cli("start", cwd=tmp_path)
    assert result.code == 3 and "highhx init" in result.stderr


def test_exec_passthrough_arguments(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import sys

    result = cli(
        "exec", "--json", sys.executable, "-c", "import sys; print(sys.argv[1:])", "-q", "--flag", cwd=tmp_path
    )
    assert result.code == 0
    assert "['-q', '--flag']" in result.json()["stdout"]


def test_exec_exit_codes(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import sys

    assert cli("exec", sys.executable, "-c", "raise SystemExit(7)", cwd=tmp_path).code == 7
    assert (
        cli("exec", "--timeout", "0.3s", sys.executable, "-c", "import time; time.sleep(5)", cwd=tmp_path).code == 124
    )
    assert cli("exec", "no-such-binary-xyz", cwd=tmp_path).code == 127


def test_dangerous_exec_needs_approval(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    result = cli("exec", "git", "push", "origin", "main", cwd=tmp_path)
    assert result.code == 6 and "Not approved" in result.stderr
    result = cli("exec", "--dry-run", "git", "push", "origin", "main", cwd=tmp_path)
    assert result.code == 0 and "would run" in result.stdout


def test_config_schema_is_json(cli) -> None:  # type: ignore[no-untyped-def]
    for kind in ("config", "workflow", "plugin"):
        result = cli("config", "schema", kind)
        assert json.loads(result.stdout)["type"] == "object"


def test_quiet_mode_suppresses_output(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    result = cli("info", "--quiet", cwd=python_project)
    assert result.code == 0 and result.stdout == ""
