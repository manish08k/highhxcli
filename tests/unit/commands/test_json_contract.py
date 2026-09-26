"""--json must always produce exactly one valid JSON document, for every command."""

import json
import sys
from pathlib import Path

import pytest
import yaml

COMMANDS = [
    ["status"],
    ["info"],
    ["doctor"],
    ["dev", "--dry-run"],
    ["start", "--dry-run"],
    ["stop"],
    ["restart", "--dry-run"],
    ["run", "demo"],
    ["run", "demo", "--dry-run"],
    ["script"],
    ["task"],
    ["watch", "--dry-run", "--", "echo"],
    ["deps"],
    ["deps", "install", "--dry-run"],
    ["deps", "clean", "--dry-run"],
    ["deps", "outdated"],
    ["deps", "audit"],
    ["build", "--dry-run", "--no-workflow"],
    ["clean", "--dry-run"],
    ["artifacts"],
    ["env"],
    ["env", "check"],
    ["env", "set", "A=1", "--dry-run"],
    ["env", "profile"],
    ["env", "diff", "development", "staging"],
    ["git"],
    ["git", "history"],
    ["git", "branch"],
    ["git", "tag"],
    ["git", "diff"],
    ["version"],
    ["changelog"],
    ["deploy", "status"],
    ["deploy", "logs"],
    ["environments"],
    ["security"],
    ["security", "secrets"],
    ["security", "config"],
    ["security", "deps"],
    ["security", "report"],
    ["services"],
    ["ports", "65000"],
    ["workflow", "list"],
    ["workflow", "validate"],
    ["workflow", "graph", "demo"],
    ["schedule", "list"],
    ["schedule", "trigger", "nightly"],
    ["hook", "list"],
    ["hook", "run", "pre-push"],
    ["trigger"],
    ["watchers"],
    ["logs"],
    ["history"],
    ["report"],
    ["plugin", "list"],
    ["plugin", "search"],
    ["config", "show"],
    ["config", "validate"],
    ["config", "get", "project.name"],
    ["config", "path"],
    ["policy", "show"],
    ["policy", "check"],
    ["policy", "validate"],
    ["workspace", "list"],
    ["profile", "list"],
    ["diagnose"],
    ["repair", "--dry-run"],
    ["debug"],
]


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import shutil

    from tests.conftest import FIXTURES, init_repo

    root = tmp_path_factory.mktemp("jsonproj") / "p"
    shutil.copytree(FIXTURES / "python_project", root, ignore=shutil.ignore_patterns("__pycache__"))
    if shutil.which("git"):
        init_repo(root)
    from highhx.cli import run
    from highhx.commands import App

    run(["init", "-q"], app=App(cwd=root))
    config_path = root / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["deploy"] = {"targets": {"local": {"type": "local", "command": "echo deploy"}}}
    config["schedules"] = [{"name": "nightly", "cron": "@daily", "run": f'"{sys.executable}" -c "pass"'}]
    config_path.write_text(yaml.safe_dump(config))
    (root / ".highhx" / "workflows" / "demo.yaml").write_text(
        f'name: demo\nsteps:\n  - id: a\n    run: \'"{sys.executable}" -c "print(1)"\'\n'
    )
    return root


@pytest.mark.parametrize("args", COMMANDS, ids=" ".join)
def test_json_output_is_a_single_document(cli, project: Path, args: list[str]) -> None:  # type: ignore[no-untyped-def]
    if "--" in args:
        index = args.index("--")
        argv = [*args[:index], "--json", *args[index:]]
    else:
        argv = [*args, "--json"]
    result = cli(*argv, cwd=project)
    document = json.loads(result.stdout)
    assert isinstance(document, dict | list)
    assert "Traceback" not in result.stderr
    if isinstance(document, dict) and document.get("error") == "unexpected":
        pytest.fail(document["message"])
