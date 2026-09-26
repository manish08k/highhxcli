from pathlib import Path

import pytest

from highhx.core.errors import ApprovalDeniedError, UsageError, ValidationError
from highhx.git.manager import GitManager
from highhx.git.repository import GitRepository
from highhx.policy.engine import PolicyEngine, PolicySet
from tests.conftest import git, requires_git

pytestmark = requires_git


@pytest.fixture
def repo(git_python_project: Path, make_engine):  # type: ignore[no-untyped-def]
    kit = make_engine(cwd=git_python_project, yes=True, interactive=False)
    return GitManager(
        kit.engine, GitRepository(kit.engine, git_python_project), PolicyEngine(PolicySet(forbidden_files=[".env"]))
    ), git_python_project


def test_status_branch_and_history(repo) -> None:  # type: ignore[no-untyped-def]
    manager, root = repo
    status = manager.status()
    assert status.branch == "main" and status.clean
    (root / "new.txt").write_text("x")
    assert manager.repo.status().untracked == ["new.txt"]
    assert manager.repo.changed_files() == ["new.txt"]
    assert manager.repo.log(limit=5)[0].subject == "feat: initial commit"


def test_commit_and_tag(repo) -> None:  # type: ignore[no-untyped-def]
    manager, root = repo
    (root / "feature.py").write_text("x = 1\n")
    sha = manager.commit("feat: add feature", all_changes=True, conventional=True)
    assert sha and manager.repo.log(limit=1)[0].subject == "feat: add feature"
    manager.tag("v1.0.0", message="first")
    assert manager.repo.tag_exists("v1.0.0")
    with pytest.raises(ValidationError):
        manager.tag("v1.0.0")


def test_commit_refuses_forbidden_files(repo) -> None:  # type: ignore[no-untyped-def]
    manager, root = repo
    (root / ".env").write_text("SECRET=1\n")
    with pytest.raises(ValidationError) as info:
        manager.commit("chore: env", all_changes=True)
    assert ".env" in info.value.details


def test_nothing_to_commit(repo) -> None:  # type: ignore[no-untyped-def]
    manager, _ = repo
    with pytest.raises(ValidationError):
        manager.commit("chore: nothing")


def test_branch_lifecycle(repo) -> None:  # type: ignore[no-untyped-def]
    manager, _ = repo
    manager.create_branch("feature/x")
    assert manager.repo.current_branch() == "feature/x"
    manager.switch("main")
    manager.delete_branch("feature/x")
    assert not manager.repo.branch_exists("feature/x")
    with pytest.raises(UsageError):
        manager.delete_branch("main")


def test_force_push_needs_interactive_approval_on_protected_branch(
    git_python_project: Path, make_engine, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(git_python_project, "remote", "add", "origin", str(remote))
    git(git_python_project, "push", "-q", "-u", "origin", "main")
    kit = make_engine(cwd=git_python_project, yes=True, interactive=False)
    manager = GitManager(kit.engine, GitRepository(kit.engine, git_python_project))
    outcome = manager.sync(push=True)
    assert outcome.pushed
    with pytest.raises(ApprovalDeniedError):
        manager.sync(force_push=True)


def test_sync_refuses_diverged_branch(git_python_project: Path, make_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    remote = tmp_path / "r.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(git_python_project, "remote", "add", "origin", str(remote))
    git(git_python_project, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "remote.txt").write_text("r")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "feat: remote")
    git(other, "push", "-q")
    (git_python_project / "local.txt").write_text("l")
    git(git_python_project, "add", "-A")
    git(git_python_project, "commit", "-qm", "feat: local")
    kit = make_engine(cwd=git_python_project, yes=True, interactive=False)
    with pytest.raises(ValidationError) as info:
        GitManager(kit.engine, GitRepository(kit.engine, git_python_project)).sync()
    assert "diverged" in info.value.message
