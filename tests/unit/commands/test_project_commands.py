import json
from pathlib import Path

import yaml


def test_init_creates_highhx_directory(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    result = cli("init", "--json", cwd=python_project)
    assert result.code == 0
    data = result.json()
    assert data["profile"]["primary"] == "python"
    config = yaml.safe_load((python_project / ".highhx" / "config.yaml").read_text())
    assert config["project"]["name"] == "pyapp" and "pytest" in config["commands"]["test"]
    for name in (
        "environment.yaml",
        "policies.yaml",
        "workflows/test.yaml",
        "workflows/build.yaml",
        "workflows/deploy.yaml",
        "workflows/rollback.yaml",
        "workflows/release.yaml",
        "workflows/dev.yaml",
    ):
        assert (python_project / ".highhx" / name).exists(), name


def test_init_never_overwrites_without_force(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=python_project)
    config = python_project / ".highhx" / "config.yaml"
    config.write_text("version: 1\nproject:\n  name: custom\n")
    result = cli("init", "--json", cwd=python_project)
    assert result.code == 0 and ".highhx/config.yaml" in result.json()["plan"]["skip"]
    assert "custom" in config.read_text()
    denied = cli("init", "--force", cwd=python_project)
    assert denied.code == 6 and "custom" in config.read_text()
    forced = cli("init", "--force", "--yes", cwd=python_project)
    assert forced.code == 0 and "pyapp" in config.read_text()


def test_init_dry_run_writes_nothing(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    result = cli("init", "--dry-run", cwd=python_project)
    assert result.code == 0 and "Would create" in result.stdout
    assert not (python_project / ".highhx").exists()


def test_status_json_and_human(cli, git_python_project: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=git_python_project)
    data = cli("status", "--json", cwd=git_python_project).json()
    assert data["project"]["initialized"] and data["git"]["branch"] == "main"
    human = cli("status", cwd=git_python_project)
    assert human.code == 0 and "Project" in human.stdout and "Git" in human.stdout


def test_doctor_and_diagnose(cli, git_python_project: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=git_python_project)
    doctor = cli("doctor", "--json", cwd=git_python_project)
    data = doctor.json()
    names = [c["name"] for c in data["checks"]]
    assert any(n.startswith("git ") for n in names) and "Configuration valid" in names
    assert doctor.code == (1 if not data["ok"] else 0)
    human = cli("doctor", cwd=git_python_project)
    assert "HighhX Doctor" in human.stdout
    (git_python_project / ".highhx" / "workflows" / "broken.yaml").write_text(
        "name: broken\nsteps:\n  - id: a\n    run: echo\n    depends_on: [zzz]\n"
    )
    diagnose = cli("diagnose", "--json", cwd=git_python_project).json()
    assert any(p["id"] == "workflow-invalid:broken" for p in diagnose["problems"])


def test_repair_restores_directories_and_gitignore(cli, git_python_project: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=git_python_project)
    import shutil

    shutil.rmtree(git_python_project / ".highhx" / "hooks")
    (git_python_project / ".gitignore").write_text("")
    result = cli("repair", "--json", cwd=git_python_project)
    repairs = {r["repair"] for r in result.json()["repairs"]}
    assert {"create-dirs", "gitignore"} <= repairs
    assert (git_python_project / ".highhx" / "hooks").is_dir()
    assert ".highhx/state/" in (git_python_project / ".gitignore").read_text()


def test_workflow_commands(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=python_project)
    listed = cli("workflow", "list", "--json", cwd=python_project).json()
    assert {w["key"] for w in listed["workflows"]} >= {"test", "build"}
    assert cli("workflow", "validate", cwd=python_project).code == 0
    graph = cli("workflow", "graph", "build", cwd=python_project)
    assert "Stage 2" in graph.stdout
    assert cli("workflow", "create", "nightly", "--template", "ci", cwd=python_project).code == 0
    assert (python_project / ".highhx" / "workflows" / "nightly.yaml").exists()
    (python_project / ".highhx" / "workflows" / "cyclic.yaml").write_text(
        "name: cyclic\nsteps:\n  - id: a\n    run: echo\n    depends_on: [b]\n  - id: b\n    run: echo\n    depends_on: [a]\n"
    )
    bad = cli("workflow", "validate", "cyclic", "--json", cwd=python_project)
    assert bad.code == 8 and "circular dependency" in json.dumps(bad.json())


def test_test_command_runs_pytest(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    import sys

    (python_project / ".highhx").mkdir()
    (python_project / ".highhx" / "config.yaml").write_text(
        f'commands:\n  test: "{sys.executable} -m pytest -q -p no:cacheprovider"\n'
    )
    result = cli("test", "--json", cwd=python_project)
    assert result.code == 0
    data = result.json()
    assert data["framework"]["name"] == "pytest" and data["summary"]["passed"] == 1
    (python_project / "tests" / "test_fail.py").write_text("def test_no():\n    assert False\n")
    failing = cli("test", "--json", cwd=python_project)
    assert failing.code == 1 and failing.json()["summary"]["failed"] == 1


def test_env_commands_never_print_secrets(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=python_project)
    secret = "HIGHHX_TEST_STRIPE_SECRET_NOT_REAL"
    assert cli("env", "set", f"STRIPE_SECRET_KEY={secret}", cwd=python_project).code == 0
    for args in (("env",), ("env", "--json"), ("env", "check", "--json"), ("env", "diff", "development", "staging")):
        output = cli(*args, cwd=python_project)
        assert secret not in output.stdout + output.stderr
    assert secret in (python_project / ".env").read_text()


def test_history_logs_and_report(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import sys

    cli("init", cwd=tmp_path)
    cli("exec", sys.executable, "-c", "print('logged line')", cwd=tmp_path)
    history = cli("history", "--json", cwd=tmp_path).json()
    exec_id = history["executions"][0]["id"]
    detail = cli("history", exec_id, "--json", cwd=tmp_path).json()
    assert detail["status"] == "success"
    logs = cli("logs", exec_id, "--json", cwd=tmp_path).json()
    assert any("logged line" in line for line in logs["lines"])
    report = cli("report", "--json", cwd=tmp_path).json()
    assert report["total"] >= 1
    assert cli("trace", exec_id, cwd=tmp_path).code == 0


def test_config_validate_reports_problems(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    (tmp_path / ".highhx" / "config.yaml").write_text("version: 1\ncomands: {}\n")
    result = cli("config", "validate", "--json", cwd=tmp_path)
    assert result.code == 3
    assert "did you mean 'commands'" in json.dumps(result.json())
