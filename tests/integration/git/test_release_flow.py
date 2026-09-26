from pathlib import Path

from tests.conftest import git, requires_git

pytestmark = requires_git


def test_full_release(cli, git_python_project: Path) -> None:  # type: ignore[no-untyped-def]
    assert cli("init", cwd=git_python_project).code == 0
    git(git_python_project, "add", "-A")
    git(git_python_project, "commit", "-qm", "chore: add highhx")
    (git_python_project / "src" / "pyapp" / "extra.py").write_text("X = 1\n")
    git(git_python_project, "add", "-A")
    git(git_python_project, "commit", "-qm", "feat(core): add extra module")
    version = cli("version", "--json", cwd=git_python_project).json()
    assert version["version"] == "1.2.3" and version["suggested_bump"] == "minor"
    dirty = git_python_project / "scratch.txt"
    dirty.write_text("x")
    refused = cli("release", "--yes", cwd=git_python_project)
    assert refused.code == 8 and "uncommitted" in refused.stderr
    dirty.unlink()
    result = cli("release", "--yes", "--json", cwd=git_python_project)
    assert result.code == 0, result.stderr
    assert result.json()["tag"] == "v1.3.0"
    assert 'version = "1.3.0"' in (git_python_project / "pyproject.toml").read_text()
    changelog = (git_python_project / "CHANGELOG.md").read_text()
    assert "## [1.3.0]" in changelog and "**core:** add extra module" in changelog
    assert git(git_python_project, "tag", "--list").split() == ["v1.3.0"]
    assert git(git_python_project, "log", "-1", "--format=%s").strip() == "chore(release): v1.3.0"


def test_release_dry_run_changes_nothing(cli, git_python_project: Path) -> None:  # type: ignore[no-untyped-def]
    before = (git_python_project / "pyproject.toml").read_text()
    result = cli("release", "patch", "--dry-run", "--json", cwd=git_python_project)
    assert result.code == 0 and result.json()["next"] == "1.2.4"
    assert (git_python_project / "pyproject.toml").read_text() == before
    assert git(git_python_project, "tag", "--list") == ""


def test_git_commands(cli, git_python_project: Path) -> None:  # type: ignore[no-untyped-def]
    assert cli("git", "--json", cwd=git_python_project).json()["clean"] is True
    assert cli("git", "branch", "feature/a", "--create", cwd=git_python_project).code == 0
    branches = cli("git", "branch", "--json", cwd=git_python_project).json()["branches"]
    assert any(b["name"] == "feature/a" and b["current"] for b in branches)
    (git_python_project / "a.txt").write_text("a")
    assert cli("git", "commit", "-m", "feat: a", "--all", cwd=git_python_project).code == 0
    history = cli("git", "history", "--json", cwd=git_python_project).json()["commits"]
    assert history[0]["type"] == "feat"
    stat = cli("git", "diff", "HEAD~1", "--stat", "--json", cwd=git_python_project).json()
    assert stat["files"][0]["path"] == "a.txt"


def test_pre_commit_hook_runs_highhx(cli, git_python_project: Path) -> None:  # type: ignore[no-untyped-def]
    import sys

    import yaml

    cli("init", cwd=git_python_project)
    config_path = git_python_project / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["hooks"] = {"pre-commit": f'"{sys.executable}" -c "raise SystemExit(1)"'}
    config_path.write_text(yaml.safe_dump(config))
    assert cli("hook", "install", "pre-commit", "--yes", cwd=git_python_project).code == 0
    assert cli("hook", "run", "pre-commit", cwd=git_python_project).code == 1
