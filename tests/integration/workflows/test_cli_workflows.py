import sys
from pathlib import Path

import yaml

from tests.conftest import copy_fixture

PY = sys.executable


def write_workflow(root: Path, doc: dict) -> None:
    (root / ".highhx" / "workflows" / f"{doc['name']}.yaml").write_text(yaml.safe_dump(doc))


def test_run_parallel_workflow_with_failure(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    write_workflow(
        tmp_path,
        {
            "name": "pipeline",
            "settings": {"fail_fast": False},
            "steps": [
                {"id": "a", "run": f'"{PY}" -c "print(1)"'},
                {"id": "b", "run": f'"{PY}" -c "raise SystemExit(2)"'},
                {"id": "c", "run": f'"{PY}" -c "print(3)"', "depends_on": ["a", "b"]},
                {"id": "notify", "run": f'"{PY}" -c "print(4)"', "depends_on": ["c"], "if": "always()"},
            ],
        },
    )
    result = cli("run", "pipeline", "--json", cwd=tmp_path)
    assert result.code == 1
    steps = {s["id"]: s["status"] for s in result.json()["steps"]}
    assert steps == {"a": "success", "b": "failed", "c": "skipped", "notify": "success"}
    record = cli("history", result.json()["execution_id"], "--json", cwd=tmp_path).json()
    assert record["status"] == "failed" and len(record["steps"]) == 4


def test_run_with_inputs_and_dry_run(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    write_workflow(
        tmp_path,
        {
            "name": "greet",
            "inputs": {"who": {"required": True}},
            "steps": [{"id": "hi", "run": f'"{PY}" -c "print(\'hi ${{{{ inputs.who }}}}\')"'}],
        },
    )
    dry = cli("run", "greet", "--input", "who=team", "--dry-run", cwd=tmp_path)
    assert dry.code == 0 and "hi team" in dry.stdout
    assert cli("run", "greet", cwd=tmp_path).code == 8
    ok = cli("run", "greet", "-i", "who=team", cwd=tmp_path)
    assert ok.code == 0 and "hi team" in ok.stdout


def test_trigger_runs_subscribed_workflows(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    write_workflow(
        tmp_path,
        {
            "name": "on-push",
            "on": ["push"],
            "steps": [{"id": "s", "run": f'"{PY}" -c "import os; print(os.environ[\'HIGHHX_EVENT\'])"'}],
        },
    )
    events = cli("trigger", "--json", cwd=tmp_path).json()["events"]
    assert events == {"push": ["on-push"]}
    result = cli("trigger", "push", cwd=tmp_path)
    assert result.code == 0 and "push" in result.stdout
    assert cli("trigger", "nothing", cwd=tmp_path).code == 4


def test_tasks_run_dependencies_first(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    config_path = tmp_path / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    order = tmp_path / "order.txt"
    append = lambda name: f"\"{PY}\" -c \"open(r'{order}', 'a').write('{name}\\n')\""  # noqa: E731
    config["tasks"] = {
        "gen": {"run": append("gen")},
        "compile": {"run": append("compile"), "depends_on": ["gen"]},
        "ship": {"run": append("ship"), "depends_on": ["compile"]},
    }
    config_path.write_text(yaml.safe_dump(config))
    assert cli("task", "ship", cwd=tmp_path).code == 0
    assert order.read_text().split() == ["gen", "compile", "ship"]


def test_workspace_run_across_members(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = copy_fixture("monorepo", tmp_path)
    cli("init", cwd=root)
    members = cli("workspace", "--json", cwd=root).json()["members"]
    assert [m["path"] for m in members] == ["packages/api", "packages/web"]
    result = cli(
        "workspace", "run", "--parallel", PY, "-c", "import os; print(os.path.basename(os.getcwd()))", cwd=root
    )
    assert result.code == 0 and "api" in result.stdout and "web" in result.stdout
