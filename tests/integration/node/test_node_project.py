from pathlib import Path

from tests.conftest import copy_fixture


def test_node_project_detection_and_commands(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = copy_fixture("node_project", tmp_path)
    assert cli("init", cwd=root).code == 0
    info = cli("info", "--json", cwd=root).json()
    assert info["profile"]["primary"] == "nextjs"
    assert info["effective_commands"]["test"] == "pnpm run test"
    deps = cli("deps", "--json", cwd=root).json()
    assert deps["managers"][0]["manager"] == "pnpm" and deps["managers"][0]["lockfile_present"]
    dry = cli("test", "--dry-run", cwd=root)
    assert dry.code == 0 and "pnpm run test" in dry.stdout
    assert cli("workflow", "validate", "--no-tool-check", cwd=root).code == 0


def test_script_runs_package_json_scripts(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = copy_fixture("node_project", tmp_path)
    listed = cli("script", "--json", cwd=root).json()["scripts"]
    assert listed["build"] == {"command": "pnpm run build", "source": "package.json"}
    assert "pnpm run lint" in cli("script", "lint", "--dry-run", cwd=root).stdout
