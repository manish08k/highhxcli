import sys
from pathlib import Path

import yaml


def configure(root: Path) -> None:
    config_path = root / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["commands"]["test"] = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'
    config["commands"]["build"] = (
        f"\"{sys.executable}\" -c \"import pathlib; pathlib.Path('dist').mkdir(exist_ok=True); pathlib.Path('dist/pyapp-1.2.3.tar.gz').write_text('x')\""
    )
    config["commands"]["lint"] = f'"{sys.executable}" -c "print(\'lint ok\')"'
    config_path.write_text(yaml.safe_dump(config))


def test_build_workflow_and_artifacts(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    assert cli("init", cwd=python_project).code == 0
    configure(python_project)
    for name in ("build.yaml", "test.yaml", "ci.yaml"):
        (python_project / ".highhx" / "workflows" / name).unlink()
    cli("workflow", "create", "build", "--template", "build", cwd=python_project)
    result = cli("build", "--json", cwd=python_project)
    assert result.code == 0, result.stdout + result.stderr
    assert result.json()["status"] == "success"
    artifacts = cli("artifacts", "--verify", "--json", cwd=python_project).json()
    assert artifacts["artifacts"][0]["path"] == "dist/pyapp-1.2.3.tar.gz" and artifacts["verification"] == []
    (python_project / "dist" / "pyapp-1.2.3.tar.gz").write_text("tampered")
    assert cli("artifacts", "--verify", cwd=python_project).code == 1


def test_check_runs_lint_and_tests_in_parallel(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=python_project)
    configure(python_project)
    result = cli("check", "--json", cwd=python_project)
    assert result.code == 0
    assert {s["id"] for s in result.json()["steps"]} == {"lint", "test"}


def test_clean_removes_build_outputs_only(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=python_project)
    (python_project / "dist").mkdir()
    (python_project / "src" / "pyapp" / "__pycache__").mkdir()
    result = cli("clean", "--json", cwd=python_project)
    assert result.code == 0
    assert set(result.json()["removed"]) == {"dist", "src/pyapp/__pycache__"}
    assert (python_project / "src" / "pyapp" / "calc.py").exists()


def test_deps_install_dry_run(cli, python_project: Path) -> None:  # type: ignore[no-untyped-def]
    result = cli("deps", "install", "--dry-run", cwd=python_project)
    assert result.code == 0 and "pip install -e ." in result.stdout
