"""filesystem.* and git.* actions against real files and a real git repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit.actions.conftest import git


# ------------------------------------------------------------ filesystem
def test_write_read_verify_and_undo(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    result = executor.run("filesystem.write", {"path": "docs/new.md", "content": "hello\n"})
    assert result.ok and result.verified is True and result.changed == ["docs/new.md"]
    assert executor.run("filesystem.read", {"path": "docs/new.md"}).output["text"] == "hello"
    executor.run("filesystem.write", {"path": "pyproject.toml", "content": "replaced\n"})
    assert executor.journal.undo_last()  # /undo restores the original
    assert "[project]" in (agent_project / "pyproject.toml").read_text()


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("../outside.txt", "outside the project"),
        ("/etc/hosts", "outside the project"),
        (".env", "may contain secrets"),
        ("config/secrets.yaml", "may contain secrets"),
        ("id_rsa", "may contain secrets"),
        (".git/config", "protected"),
        (".highhx/state/x", "protected"),
    ],
)
def test_writes_are_confined(agent_project: Path, executor_for, path: str, why: str) -> None:
    executor, _ = executor_for(agent_project)
    result = executor.run("filesystem.write", {"path": path, "content": "x"})
    assert not result.ok and why in result.error
    assert result.attempts == 1


def test_secret_files_are_never_read(agent_project: Path, executor_for) -> None:
    (agent_project / ".env").write_text("TOKEN=abc\n")
    executor, _ = executor_for(agent_project)
    result = executor.run("filesystem.read", {"path": ".env"})
    assert not result.ok and "never reads or changes secret files" in result.error  # user wording, not AI
    assert "abc" not in str(result.to_dict())


def test_copy_move_delete_and_their_compensations(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    copied = executor.run("filesystem.copy", {"source": "pyproject.toml", "destination": "copy.toml"})
    assert copied.ok and (agent_project / "copy.toml").exists()
    assert executor.run("filesystem.copy", {"source": "pyproject.toml", "destination": "copy.toml"}).error
    moved = executor.run("filesystem.move", {"source": "copy.toml", "destination": "moved.toml"})
    assert moved.ok and (agent_project / "moved.toml").exists()
    move_plan = executor.plan("filesystem.move", {"source": "copy.toml", "destination": "moved.toml"})
    assert "moved moved.toml back" in (executor.compensate(move_plan, moved) or "")
    deleted = executor.run("filesystem.delete", {"path": "copy.toml"})
    assert deleted.ok and deleted.verified and not (agent_project / "copy.toml").exists()
    delete_plan = executor.plan("filesystem.delete", {"path": "pyproject.toml"})
    assert "restored copy.toml" in (executor.compensate(delete_plan, deleted) or "")
    assert (agent_project / "copy.toml").exists()


def test_directories_need_recursive_and_are_critical(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project)
    (agent_project / "scratch").mkdir()
    assert "recursive" in executor.run("filesystem.delete", {"path": "scratch"}).error
    plan = executor.plan("filesystem.delete", {"path": "scratch", "recursive": True})
    assert plan.decision.risk.label == "critical"
    ui.action_answers = [False]
    assert executor.run("filesystem.delete", {"path": "scratch", "recursive": True}).status == "denied"
    assert (agent_project / "scratch").is_dir()


def test_search_respects_gitignore(repo: Path, executor_for) -> None:
    (repo / ".gitignore").write_text("generated/\n*.log\n")
    (repo / "generated").mkdir()
    (repo / "generated" / "out.py").write_text("NEEDLE\n")
    (repo / "debug.log").write_text("NEEDLE\n")
    (repo / "kept.py").write_text("NEEDLE\n")
    (repo / ".env").write_text("NEEDLE=1\n")
    executor, _ = executor_for(repo)
    matches = executor.run("filesystem.search", {"pattern": "NEEDLE"}).output["matches"]
    assert [m["path"] for m in matches] == ["kept.py"]


def test_search_rejects_bad_patterns(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    assert "invalid regular expression" in executor.run("filesystem.search", {"pattern": "("}).error


# ------------------------------------------------------------------- git
def test_commit_and_its_undo(repo: Path, executor_for) -> None:
    executor, _ = executor_for(repo)
    (repo / "feature.py").write_text("x = 1\n")
    before = git(repo, "rev-parse", "HEAD")
    commit = executor.run("git.commit", {"message": "feat: add feature", "all": True})
    assert commit.ok and commit.output["parent"] == before and git(repo, "rev-parse", "HEAD") == commit.output["commit"]
    plan = executor.plan("git.commit", {"message": "feat: add feature"})
    assert "changes kept" in (executor.compensate(plan, commit) or "")
    assert git(repo, "rev-parse", "HEAD") == before and (repo / "feature.py").exists()


def test_commit_undo_refuses_when_head_moved(repo: Path, executor_for) -> None:
    executor, _ = executor_for(repo)
    (repo / "a.py").write_text("a\n")
    commit = executor.run("git.commit", {"message": "feat: a", "all": True})
    (repo / "b.py").write_text("b\n")
    git(repo, "add", "b.py")
    git(repo, "commit", "-qm", "another")
    plan = executor.plan("git.commit", {"message": "feat: a"})
    assert "HEAD moved" in (executor.compensate(plan, commit) or "")


def test_tag_checkout_and_undo(repo: Path, executor_for) -> None:
    executor, _ = executor_for(repo)
    tag = executor.run("git.tag", {"name": "v9.9.9", "message": "release"})
    assert tag.ok and "v9.9.9" in git(repo, "tag")
    assert "deleted tag" in (executor.compensate(executor.plan("git.tag", {"name": "v9.9.9"}), tag) or "")
    assert "v9.9.9" not in git(repo, "tag")
    switched = executor.run("git.checkout", {"ref": "topic", "create": True})
    assert switched.ok and git(repo, "branch", "--show-current") == "topic"
    executor.compensate(executor.plan("git.checkout", {"ref": "topic"}), switched)
    assert git(repo, "branch", "--show-current") == "main"


def test_push_is_high_risk_and_needs_approval(repo: Path, executor_for, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    executor, ui = executor_for(repo)
    ui.action_answers = [False]
    denied = executor.run("git.push", {"branch": "main"})
    assert denied.status == "denied" and ui.requests[0].risk_name == "high"
    assert subprocess.run(["git", "-C", str(remote), "rev-parse", "main"], capture_output=True, check=False).returncode
    pushed = executor.run("git.push", {"branch": "main"})
    assert pushed.ok and git(repo, "rev-parse", "main") == git(remote, "rev-parse", "main")


def test_git_status_and_diff_run_the_real_commands(repo: Path, executor_for) -> None:
    executor, _ = executor_for(repo)
    (repo / "pyproject.toml").write_text("changed\n")
    status = executor.run("git.status")
    diff = executor.run("git.diff", {"stat": True})
    assert status.ok and status.output["command"] == "highhx git status"
    assert diff.ok and diff.output["command"] == "highhx git diff --stat"
