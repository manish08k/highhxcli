"""The HighhX console: slash commands map to CLI commands (no second execution path), plain text
runs a task, the palette and help, unknown commands, leaving."""

from __future__ import annotations

from pathlib import Path

from highhx.commands import App
from highhx.commands.computer_use.tui import COMMANDS, HighhxConsole, palette
from highhx.core.context import Options


def console(tmp_path: Path, lines: list[str] | None = None) -> tuple[HighhxConsole, list[list[str]]]:
    ran: list[list[str]] = []
    feed = iter(lines or [])

    def read(_prompt: str) -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    app = App(Options(interactive=False), cwd=tmp_path)
    return HighhxConsole(app, read_line=read, run_cli=lambda argv: ran.append(argv) or 0), ran


def test_lines_map_to_cli_commands(tmp_path: Path) -> None:
    tui, ran = console(tmp_path)
    tui.dispatch("export the invoices")
    tui.dispatch("/run fill the form --plan steps.yaml")
    tui.dispatch("/resume task_abc")
    tui.dispatch("/replay invoices")
    tui.dispatch("/state browser --screenshot")
    tui.dispatch("/sandbox create --isolation workspace")
    tui.dispatch("/benchmark browser -n 2")
    tui.dispatch("/cli git status")
    tui.dispatch("/traces")
    tui.dispatch("/trace tr_123")
    assert ran == [
        ["agent", "loop", "export the invoices"],
        ["agent", "loop", "fill", "the", "form", "--plan", "steps.yaml"],
        ["agent", "loop", "--resume", "task_abc"],
        ["replay", "invoices"],
        ["computer", "state", "--surface", "browser", "--screenshot"],
        ["sandbox", "create", "--isolation", "workspace"],
        ["benchmark", "run", "browser", "-n", "2"],
        ["git", "status"],
        ["trace", "list"],
        ["trace", "show", "tr_123"],
    ]


def test_console_only_commands(tmp_path: Path, capsys) -> None:
    tui, ran = console(tmp_path)
    tui.dispatch("/help")
    tui.dispatch("/palette trace")
    tui.dispatch("/nope")
    tui.dispatch("/trace")  # no trace yet
    tui.dispatch("/run")  # missing goal
    tui.dispatch('/run "unterminated')
    out = capsys.readouterr()
    text = out.out + out.err
    assert "Ctrl-C" in text and "/trace [ID]" in text and "Unknown command /nope" in text
    assert "No task trace yet" in text and "Usage: /run" in text and "Cannot read that line" in text
    assert ran == [] and tui.dispatch("/quit") is False and tui.dispatch("/exit") is False


def test_palette_filters_by_every_word() -> None:
    assert [c.name for c in palette("trace")][:2] == ["trace", "traces"]
    assert [c.name for c in palette("browser workflow")] == ["replay", "record", "workflows"]
    assert len(palette("")) == len(COMMANDS)


def test_the_loop_runs_until_end_of_input(tmp_path: Path) -> None:
    tui, ran = console(tmp_path, ["/drivers", "", "/history"])
    assert tui.run() == 0 and ran == [["computer", "drivers"], ["trajectories", "list"]]
