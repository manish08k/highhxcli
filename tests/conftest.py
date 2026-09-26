"""Shared fixtures.

Every test runs with isolated HighhX user directories, no interactive prompts,
no colors and a deterministic git identity, so the suite never depends on the
developer's personal environment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from highhx.approvals.manager import ApprovalManager
from highhx.approvals.policy import ApprovalPolicy
from highhx.core.context import ExecutionContext, Options
from highhx.core.engine import Engine
from highhx.policy.engine import PolicyEngine, PolicySet
from highhx.storage.database import Database
from highhx.storage.history import HistoryStore
from highhx.storage.logs import LogStore
from highhx.ui.prompts import StaticPrompter

FIXTURES = Path(__file__).parent / "fixtures"
PY = sys.executable


ISOLATED_VARS = ("HIGHHX_DATA_DIR", "HIGHHX_CONFIG_DIR", "HIGHHX_CACHE_DIR")


@pytest.fixture(autouse=True, scope="session")
def session_isolation(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Isolate user-level HighhX directories for the whole session.

    Module- and session-scoped fixtures run before function-scoped ones, so they need
    this baseline too. The run fails if anything touched the real user directory.
    """
    from highhx.utils.paths import user_data_dir

    real = user_data_dir()
    existed = real.exists()
    base = tmp_path_factory.mktemp("session-home")
    with pytest.MonkeyPatch.context() as mp:
        for name in ISOLATED_VARS:
            mp.setenv(name, str(base / name.lower()))
        mp.setenv("HIGHHX_NON_INTERACTIVE", "1")
        mp.setenv("GIT_CONFIG_GLOBAL", str(base / "gitconfig"))
        (base / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")
        for key, value in (
            ("GIT_AUTHOR_NAME", "HighhX Test"),
            ("GIT_AUTHOR_EMAIL", "test@example.invalid"),
            ("GIT_COMMITTER_NAME", "HighhX Test"),
            ("GIT_COMMITTER_EMAIL", "test@example.invalid"),
        ):
            mp.setenv(key, value)
        yield
    assert existed or not real.exists(), f"tests wrote to the real user data directory {real}"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HIGHHX_DATA_DIR", str(home / "data"))
    monkeypatch.setenv("HIGHHX_CONFIG_DIR", str(home / "config"))
    monkeypatch.setenv("HIGHHX_CACHE_DIR", str(home / "cache"))
    monkeypatch.setenv("HIGHHX_NON_INTERACTIVE", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    for name in ("HIGHHX_ENV", "HIGHHX_PROFILE", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "HighhX Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "HighhX Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    (home / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def init_repo(root: Path, message: str = "feat: initial commit") -> None:
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


def copy_fixture(name: str, dest: Path) -> Path:
    target = dest / name
    shutil.copytree(FIXTURES / name, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    return target


@pytest.fixture
def python_project(tmp_path: Path) -> Path:
    return copy_fixture("python_project", tmp_path)


@pytest.fixture
def git_python_project(python_project: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    init_repo(python_project)
    return python_project


@dataclass
class CliResult:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Callable[..., CliResult]:
    """Invoke the CLI in-process: ``cli("status", "--json", cwd=path)``."""
    from highhx.cli import run
    from highhx.commands import App

    def _run(*args: str, cwd: Path | None = None) -> CliResult:
        capsys.readouterr()
        app = App(cwd=cwd)
        code = run(list(args), app=app)
        captured = capsys.readouterr()
        return CliResult(code, captured.out, captured.err)

    return _run


@dataclass
class EngineKit:
    engine: Engine
    prompter: StaticPrompter
    history: HistoryStore
    logs: LogStore
    db: Database


@pytest.fixture
def make_engine(tmp_path: Path) -> Iterator[Callable[..., EngineKit]]:
    created: list[Database] = []

    def _make(
        *,
        yes: bool = False,
        dry_run: bool = False,
        interactive: bool = True,
        answer: bool = True,
        policy: PolicySet | None = None,
        approval_policy: ApprovalPolicy | None = None,
        cwd: Path | None = None,
    ) -> EngineKit:
        options = Options(yes=yes, dry_run=dry_run, interactive=interactive)
        ctx = ExecutionContext(options=options, cwd=cwd or tmp_path)
        prompter = StaticPrompter(interactive=interactive, answer=answer)
        approvals = ApprovalManager(approval_policy or ApprovalPolicy(), prompter, assume_yes=yes, dry_run=dry_run)
        db = Database.open(":memory:")
        created.append(db)
        history = HistoryStore(db)
        logs = LogStore(tmp_path / "logs")
        engine = Engine(ctx, approvals=approvals, policy=PolicyEngine(policy), history=history, logs=logs)
        return EngineKit(engine, prompter, history, logs, db)

    yield _make
    for db in created:
        db.close()


def py_cmd(code: str) -> list[str]:
    """argv running Python code with the test interpreter (portable)."""
    return [PY, "-c", code]
