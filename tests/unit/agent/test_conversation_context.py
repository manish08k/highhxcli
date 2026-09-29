"""The interactive session remembers what it acted on: "open it", "the second one" and "no, I
meant …" resolve from that — through the same executor, verification and trace as any request."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from highhx.commands import App
from tests.unit.agent.conftest import agent_project, make_app  # noqa: F401
from tests.unit.agent.test_interactive_shell import free_repl
from tests.unit.automation.fakes import browser  # noqa: F401


def opened_paths(record: dict[str, Any]) -> list[str]:
    return [argv[-1] for argv in record["argv"]]


def opened_urls(record: dict[str, Any]) -> list[str]:
    return [flow["open"] for flow in record["flows"] if "open" in flow]


def test_a_free_conversation_resolves_references_from_what_ran(
    agent_project: Path,  # noqa: F811
    make_app: Callable[..., App],  # noqa: F811
    browser: dict[str, Any],  # noqa: F811
) -> None:
    (agent_project / "docs").mkdir()
    (agent_project / "docs" / "architecture.md").write_text("# architecture\n")
    (agent_project / "docs" / "plan.pdf").write_bytes(b"%PDF")
    (agent_project / "reports").mkdir()
    (agent_project / "reports" / "q3.pdf").write_bytes(b"%PDF")
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(
        app,
        "open it",  # nothing yet: asked, nothing runs
        "find the architecture document",
        "open it",
        "open the pdf",  # two PDFs: a question, nothing runs
        "the second one",
        "open github",
        "open it",
        "no, I meant youtube",
        "/quit",
    )
    assert repl.run() == 0
    out = buffer.getvalue()
    assert "Nothing earlier in this conversation for 'it' to refer to" in out
    assert "I don't know an app or website called 'it'" not in out
    assert "1 file(s): docs/architecture.md" in out  # filesystem.find ran through the executor

    paths = opened_paths(browser)
    assert paths[0].endswith("docs/architecture.md")
    assert "Which one do you mean?" in out and "1." in out and "2." in out
    assert len(paths) == 2 and paths[1].endswith(".pdf")  # the second candidate, once answered

    assert opened_urls(browser) == ["https://github.com", "https://github.com", "https://www.youtube.com"]
    last = repl.memory.turns[-1]
    assert last.entities[0].value == "https://www.youtube.com" and last.actions == ("browser.open",)
    assert "Unexpected error" not in out


def test_a_failed_step_is_not_remembered(
    agent_project: Path,  # noqa: F811
    make_app: Callable[..., App],  # noqa: F811
    browser: dict[str, Any],  # noqa: F811
) -> None:
    (agent_project / "tool.sh").write_text("echo hi\n")  # executable: opening it is refused
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "open tool.sh", "open it", "/quit")
    assert repl.run() == 0
    assert browser["argv"] == []
    assert repl.memory.turns[-1].entities == ()
    assert "The last step did not produce anything 'it' could refer to" in buffer.getvalue()


def test_clear_forgets_the_conversation(
    agent_project: Path,  # noqa: F811
    make_app: Callable[..., App],  # noqa: F811
    browser: dict[str, Any],  # noqa: F811
) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "open github", "/clear", "open it", "/quit")
    assert repl.run() == 0
    assert opened_urls(browser) == ["https://github.com"]
    assert "Nothing earlier in this conversation" in buffer.getvalue()
