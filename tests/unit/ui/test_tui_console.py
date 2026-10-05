"""The HighhX console: slash commands map to CLI commands (no second execution path), plain text
runs a task, the palette and help, unknown commands, leaving."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


# ------------------------------------------------------------ a real terminal, real keystrokes
class Terminal:
    """``highhx tui`` in a pseudo-terminal: keys go in as a person types them (Tab, Ctrl-C, Ctrl-D)."""

    def __init__(self, cwd: Path) -> None:
        import fcntl
        import pty
        import struct
        import termios

        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # pragma: no cover - child
            os.chdir(cwd)
            env = {k: v for k, v in os.environ.items() if k not in ("HIGHHX_NON_INTERACTIVE", "COLUMNS", "LINES")}
            env.update({"NO_COLOR": "1", "TERM": "xterm", "HIGHHX_BANNER": "compact"})
            os.execve(sys.executable, [sys.executable, "-m", "highhx", "tui"], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.output = b""

    def read_until(self, marker: bytes, timeout: float = 30) -> bytes:
        import select

        start, deadline = len(self.output), time.monotonic() + timeout
        while time.monotonic() < deadline and marker not in self.output[start:]:
            if select.select([self.fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.output += chunk
        assert marker in self.output[start:], self.output[start:].decode("utf-8", "replace")[-2000:]
        return self.output[start:]

    def send(self, keys: bytes) -> None:
        os.write(self.fd, keys)

    def close(self) -> int:
        import signal

        deadline = time.monotonic() + 15
        while (done := os.waitpid(self.pid, os.WNOHANG)) == (0, 0):
            if time.monotonic() > deadline:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
                os.close(self.fd)
                raise AssertionError("the console did not exit")
            with contextlib.suppress(AssertionError):
                self.read_until(b"\0", timeout=0.2)
        os.close(self.fd)
        return os.waitstatus_to_exitcode(done[1])


PROMPT = "❯ ".encode()  # noqa: RUF001


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
def test_the_console_in_a_real_terminal(tmp_path: Path) -> None:
    (tmp_path / ".highhx" / "workflows").mkdir(parents=True)
    (tmp_path / ".highhx" / "workflows" / "slow.yaml").write_text(
        'name: slow\nsteps:\n  - id: wait\n    run: "sleep 31.5"\n'
    )
    term = Terminal(tmp_path)
    try:
        term.read_until(PROMPT)
        term.send(b"/hel\t")  # Tab completes the command
        term.read_until(b"/help")
        term.send(b"\r")
        assert b"Keyboard" in term.read_until(PROMPT)
        term.send(b"not a command, never run\x03")  # Ctrl-C at the prompt drops the line
        after = term.read_until(PROMPT)
        term.send(b"/hlep\r")
        answer = term.read_until(PROMPT)
        assert b"Unknown command /hlep" in answer and b"Did you mean /help" in answer
        assert b"agent" not in after.lower() and b"task" not in after.lower()  # the dropped line did not run
        term.send(b"/cli run slow\r")  # a long command, then Ctrl-C cancels it and the console stays
        time.sleep(3)
        started = time.monotonic()
        term.send(b"\x03")
        term.read_until(PROMPT, timeout=15)
        assert time.monotonic() - started < 10
        time.sleep(0.5)
        leftover = subprocess.run(["pgrep", "-f", "sleep 31.5"], capture_output=True, text=True, check=False)
        assert leftover.stdout.strip() == "", "the cancelled command's process is still running"
        term.send(b"/help\r")  # still answering
        term.read_until(b"Keyboard")
        term.read_until(PROMPT)
        term.send(b"\x04")  # Ctrl-D leaves
    finally:
        code = term.close()
    assert code == 0


def test_tab_is_bound_in_the_line_editors_own_syntax_and_typos_are_suggested() -> None:
    from highhx.agent.input import tab_binding
    from highhx.commands.computer_use.tui import suggest

    # Regression: only GNU readline's syntax was bound; macOS (libedit) typed whitespace on Tab
    assert (
        tab_binding("Importing this module enables command line editing using libedit readline.")
        == "bind ^I rl_complete"
    )
    assert tab_binding("Importing this module enables command line editing using GNU readline.") == "tab: complete"
    assert (suggest("hlep"), suggest("trcae"), suggest("exti"), suggest("zzzz")) == ("help", "trace", "quit", "")
