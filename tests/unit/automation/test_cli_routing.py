"""Regression tests for plain language on the command line (HighhX Free, no account, no AI):
``highhx "…"``, ``highhx agent "…"`` and ``highhx do`` share one path — the deterministic decision, the JSON plan,
the executor, verification and a run trace — while every real command keeps working."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from highhx.observability.runs import RunStore
from highhx.storage.database import Database
from highhx.utils.paths import user_data_dir
from tests.unit.automation.fakes import FakeRuntime


def last_run(root: Path) -> dict[str, Any]:
    from highhx.commands import App

    app = App(cwd=root)
    path = app.paths.db_file if app.initialized else user_data_dir() / "history.db"
    db = Database.open(path)
    try:
        found = RunStore(db).list(1)
    finally:
        db.close()
    assert found, "no run recorded"
    return found[0]


@pytest.mark.parametrize("prefix", [(), ("agent",), ("agent", "run"), ("do",)])
def test_show_git_status_everywhere(cli: Callable[..., Any], repo: Path, prefix: tuple[str, ...]) -> None:
    result = cli(*prefix, "show git status", cwd=repo)
    assert result.code == 0, result.stderr
    assert "git status" in result.stdout and "verified" in result.stdout
    assert "No such command" not in result.stderr


def test_unquoted_words_are_a_request_too(cli: Callable[..., Any], repo: Path) -> None:
    result = cli("show", "git", "status", cwd=repo)
    assert result.code == 0 and "git status" in result.stdout


def test_json_output_carries_the_decision_the_plan_and_the_result(cli: Callable[..., Any], repo: Path) -> None:
    result = cli("show git status", "--json", cwd=repo)
    assert result.code == 0
    data = result.json()
    assert data["ok"] and data["run_id"].startswith("run_")
    assert data["decision"]["decision"] == "deterministic" and data["decision"]["plan"]["version"] == "1"
    assert data["result"]["steps"][0]["verification"]["status"] == "verified"


def test_open_gmail(cli: Callable[..., Any], repo: Path, browser: dict[str, Any]) -> None:
    result = cli("open Gmail", cwd=repo)
    assert result.code == 0, result.stdout + result.stderr
    assert browser["flows"] == [{"open": "https://mail.google.com/"}]
    assert "verified" in result.stdout


def test_open_gmail_signed_out_is_reported_not_faked(
    cli: Callable[..., Any], repo: Path, browser: dict[str, Any]
) -> None:
    browser["redirect"]["https://mail.google.com/"] = "https://accounts.google.com/v3/signin/identifier"
    result = cli("open Gmail and search internship", cwd=repo)
    assert result.code == 1
    assert "asks you to sign in" in result.stdout and "never signs in for you" in result.stdout
    assert len(browser["flows"]) == 1  # the search was not attempted
    assert "Stopped at step_1" in result.stdout


def test_open_gmail_and_search(cli: Callable[..., Any], repo: Path, browser: dict[str, Any]) -> None:
    result = cli("open Gmail and search internship", cwd=repo)
    assert result.code == 0, result.stdout
    assert browser["flows"] == [
        {"open": "https://mail.google.com/"},
        {"open": "https://mail.google.com/mail/u/0/#search/internship"},
    ]
    assert "2/2 steps · verified" in result.stdout


def test_open_youtube(cli: Callable[..., Any], repo: Path, browser: dict[str, Any]) -> None:
    assert cli("open YouTube", cwd=repo).code == 0
    assert browser["flows"] == [{"open": "https://www.youtube.com"}]


def test_play_lofi_on_youtube(cli: Callable[..., Any], repo: Path, media: FakeRuntime) -> None:
    result = cli("play lofi on YouTube", cwd=repo)
    assert result.code == 0, result.stdout
    assert media.visited[-1] == "https://www.youtube.com/watch?v=abc123"
    assert "playing" in result.stdout


def test_open_youtube_and_play(cli: Callable[..., Any], repo: Path, media: FakeRuntime) -> None:
    result = cli("open YouTube and play Adhento Gani", cwd=repo)
    assert result.code == 0, result.stdout
    assert "3/3 steps" in result.stdout
    run = last_run(repo)
    assert run["status"] == "succeeded" and [s["action"] for s in run["steps"]] == ["open", "search", "play"]


def test_run_the_tests(cli: Callable[..., Any], repo: Path) -> None:
    result = cli("run the tests", cwd=repo)
    assert result.code == 0, result.stdout
    assert "passed" in result.stdout


def test_create_a_file(cli: Callable[..., Any], repo: Path) -> None:
    result = cli("create a file called hello.py", cwd=repo)
    assert result.code == 0 and (repo / "hello.py").is_file()
    again = cli("create a file called hello.py", cwd=repo)  # never overwritten
    assert again.code == 1 and "already exists" in again.stdout + again.stderr


def test_open_ended_code_changes_escalate_to_pro(cli: Callable[..., Any], repo: Path) -> None:
    before = {p.name for p in repo.iterdir()}
    result = cli("refactor the authentication module and improve the architecture", cwd=repo)
    assert result.code == 10  # HighhX Pro: the account is required, nothing ran
    assert {p.name for p in repo.iterdir()} == before
    assert "◉" not in result.stdout
    assert last_run(repo)["status"] == "escalated"


def test_do_plan_shows_the_json_plan_and_runs_nothing(cli: Callable[..., Any], repo: Path) -> None:
    result = cli("do", "--plan", "--json", "create a folder called build2 and run the tests", cwd=repo)
    assert result.code == 0 and not (repo / "build2").exists()
    plan = result.json()["plan"]
    assert [s["catalog_action"] for s in plan["steps"]] == ["filesystem.create", "project.test"]
    assert plan["risk"] == "controlled"


def test_real_commands_still_win(cli: Callable[..., Any], repo: Path) -> None:
    assert cli("status", "--json", cwd=repo).code == 0
    assert cli("git", "status", cwd=repo).code == 0  # `git status` is the git command, not a request
    unknown = cli("stauts", cwd=repo)
    assert unknown.code == 2  # a single unknown word is still a usage error


def test_runs_commands(cli: Callable[..., Any], repo: Path) -> None:
    assert cli("show git status", cwd=repo).code == 0
    listing = cli("runs", "--json", cwd=repo)
    assert listing.code == 0 and listing.json()[0]["request"] == "show git status"
    shown = cli("runs", "show", "--json", cwd=repo)
    assert shown.code == 0 and shown.json()["steps"][0]["catalog_action"] == "git.status"
    stats = cli("runs", "stats", "--json", cwd=repo)
    assert stats.code == 0 and stats.json()["successful_runs"] >= 1
    assert json.dumps(stats.json())
