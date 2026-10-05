"""highhx tui — the HighhX console for computer-use work.

    ╭ HighhX  my-app · main · Pro · me@example.com · computer: local ──╮
    │ TASK   no task yet   idle                                        │
    ╰──────────────────────────────────────────────────────── /help ───╯
    > export the invoices on shop.test          (plain text: run it as a task)
    > /trace                                    (the last task's trace)
    > /                                         (Tab: the command palette)

Every command is a HighhX CLI command run in this process (``/trace show X`` is ``highhx trace
show X``), so the console adds no way to act: tasks run on the executor with approvals asked
here, and the live dashboard (``highhx.ui.live``) only watches the event stream. Ctrl-C cancels
the running task (it stays resumable). Ctrl-D, or /quit, leaves.
"""

from __future__ import annotations

import contextlib
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click

from highhx.commands import App, pass_app

if TYPE_CHECKING:
    from rich.console import Console


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    summary: str
    argv: Callable[[list[str]], list[str]] | None = None
    """How the console line maps to CLI arguments (None: a console-only command)."""


COMMANDS: tuple[Command, ...] = (
    Command(
        "run",
        "/run GOAL [--plan FILE] [--model]",
        "Work toward a goal (agent loop, live dashboard).",
        lambda a: ["agent", "loop", *a],
    ),
    Command(
        "resume",
        "/resume TASK_ID",
        "Continue an interrupted task from its checkpoint.",
        lambda a: ["agent", "loop", "--resume", *a],
    ),
    Command(
        "replay",
        "/replay WORKFLOW|TASK_ID",
        "Replay a browser workflow or a past task (self-healing).",
        lambda a: ["replay", *a],
    ),
    Command("record", "/record NAME [--url URL]", "Record a browser workflow.", lambda a: ["browser", "record", *a]),
    Command("workflows", "/workflows", "Recorded browser workflows.", lambda a: ["browser", "workflows", *a]),
    Command("trace", "/trace [ID]", "A task trace (default: the last task's).", None),
    Command("traces", "/traces", "Recent task traces.", lambda a: ["trace", "list", *a]),
    Command("history", "/history", "Recent tasks (trajectories).", lambda a: ["trajectories", "list", *a]),
    Command(
        "state",
        "/state [browser|desktop|android]",
        "Observe a surface (structure; --screenshot, --ocr …).",
        lambda a: ["computer", "state", *(["--surface", a[0], *a[1:]] if a and not a[0].startswith("-") else a)],
    ),
    Command(
        "ground",
        "/ground TARGET [--surface S]",
        "Find a target by every representation.",
        lambda a: ["computer", "ground", *a],
    ),
    Command(
        "drivers", "/drivers", "Drivers and sandbox backends available here.", lambda a: ["computer", "drivers", *a]
    ),
    Command(
        "android",
        "/android SUBCOMMAND …",
        "Android devices (devices, observe, tap, agent …).",
        lambda a: ["android", *a],
    ),
    Command(
        "sandbox",
        "/sandbox SUBCOMMAND …",
        "Sandboxes (create, list, exec, patch, apply, destroy).",
        lambda a: ["sandbox", *a],
    ),
    Command("benchmark", "/benchmark SUITE [-n RUNS]", "Run a benchmark suite.", lambda a: ["benchmark", "run", *a]),
    Command("cli", "/cli ARGS …", "Any HighhX command, e.g. /cli git status.", list),
    Command("palette", "/palette [WORDS]", "Find a command.", None),
    Command("help", "/help", "Commands and keyboard shortcuts.", None),
    Command("clear", "/clear", "Clear the screen.", None),
    Command("quit", "/quit", "Leave the console (or Ctrl-D).", None),
)
BY_NAME = {c.name: c for c in COMMANDS}
ALIASES = {"exit": "quit", "q": "quit", "?": "help", "p": "palette"}
SHORTCUTS = (
    ("Enter", "run the line: a /command, or plain text as a task"),
    ("Tab", "complete a /command (type / then Tab for the palette)"),
    ("↑ / ↓, Ctrl-R", "history and search (kept between sessions)"),
    ("Ctrl-C", "cancel the running task (it stays resumable); at the prompt: clear the line"),
    ("Ctrl-D", "leave"),
)


def palette(query: str = "") -> list[Command]:
    """Commands matching every word of ``query`` (in the name, usage or summary)."""
    words = query.lower().split()
    return [c for c in COMMANDS if all(w in f"{c.name} {c.usage} {c.summary}".lower() for w in words)]


def suggest(name: str) -> str:
    """The command a mistyped name most likely meant ("hlep" → "help"), or ''."""
    import difflib

    found = difflib.get_close_matches(name, [*BY_NAME, *ALIASES], n=1, cutoff=0.6)
    if found:
        return ALIASES.get(found[0], found[0])
    matches = palette(name)
    return matches[0].name if matches else ""


class HighhxConsole:
    def __init__(
        self,
        app: App,
        *,
        read_line: Callable[[str], str] | None = None,
        run_cli: Callable[[list[str]], int] | None = None,
    ) -> None:
        self.app = app
        self.out = app.output
        self.read_line = read_line or input
        self.run_cli = run_cli or self._run_cli
        self.last_trace = ""
        self.last_code = 0

    @property
    def console(self) -> Console:
        return self.out.console

    def _run_cli(self, argv: list[str]) -> int:
        from highhx.cli import run

        flags = [
            *(["--yes"] if self.app.options.yes else []),
            *(["--debug"] if getattr(self.app.options, "debug", False) else []),
        ]
        return run([*flags, *argv], app=App(cwd=self.app.root))

    # ----------------------------------------------------------------- screens
    def banner(self) -> None:
        from highhx.computer_use import dashboard_state
        from highhx.ui.live import render

        self.out.print(render(dashboard_state(self.app)))
        self.out.note("Type a goal to run it, /help for commands, / and Tab for the palette.")

    def help(self) -> None:
        self.out.heading("Commands")
        self.out.table(["command", "what it does"], [(c.usage, c.summary) for c in COMMANDS])
        self.out.heading("Keyboard")
        self.out.table(["key", "action"], list(SHORTCUTS))

    def show_palette(self, query: str) -> None:
        found = palette(query)
        self.out.table(["command", "what it does"], [(c.usage, c.summary) for c in found]) if found else self.out.warn(
            f"No command matches {query!r}."
        )

    # ---------------------------------------------------------------- dispatch
    def dispatch(self, line: str) -> bool:
        """Handle one line. Returns False to leave."""
        line = line.strip()
        if not line:
            return True
        if not line.startswith("/"):
            return self._cli(["agent", "loop", line])
        try:
            parts = shlex.split(line[1:])
        except ValueError as exc:
            self.out.error(f"Cannot read that line: {exc}")
            return True
        if not parts:
            self.show_palette("")
            return True
        name = ALIASES.get(parts[0], parts[0])
        args = parts[1:]
        command = BY_NAME.get(name)
        if command is None:
            close = suggest(name)
            self.out.error(f"Unknown command /{name}." + (f" Did you mean /{close}?" if close else " Try /help."))
            return True
        if name == "quit":
            return False
        if name == "help":
            self.help()
        elif name == "palette":
            self.show_palette(" ".join(args))
        elif name == "clear":
            self.console.clear()
            self.banner()
        elif name == "trace":
            target = args[0] if args else self._latest_trace()
            if not target:
                self.out.warn("No task trace yet: run a task first.")
            else:
                self._cli(["trace", "show", target])
        elif command.argv is not None:
            if name in ("run", "resume", "replay", "ground", "record") and not args:
                self.out.error(f"Usage: {command.usage}")
            else:
                self._cli(command.argv(args))
        return True

    def _cli(self, argv: list[str]) -> bool:
        try:
            self.last_code = self.run_cli(argv)
        except KeyboardInterrupt:
            self.out.warn("Cancelled.")
            self.last_code = 130
        return True

    def _latest_trace(self) -> str:
        from highhx.observability.tasktrace import TraceStore

        recent = TraceStore.for_app(self.app).recent(1)
        return str(recent[0]["trace_id"]) if recent else ""

    # -------------------------------------------------------------------- loop
    def run(self) -> int:
        from highhx.agent.input import setup_readline

        save = setup_readline()
        self._complete()
        self.banner()
        try:
            while True:
                try:
                    line = self.read_line("❯ ")  # noqa: RUF001
                except KeyboardInterrupt:
                    self.out.plain("")
                    continue
                except EOFError:
                    self.out.plain("")
                    break
                if not self.dispatch(line):
                    break
        finally:
            save()
        return 0

    def _complete(self) -> None:
        with contextlib.suppress(ImportError):
            import readline

            names = [f"/{c.name}" for c in COMMANDS]

            def complete(text: str, state: int) -> str | None:
                buffer = readline.get_line_buffer()
                if not buffer.startswith("/") or " " in buffer:
                    return None
                matches = [n for n in names if n.startswith(text or "/")]
                return matches[state] if state < len(matches) else None

            readline.set_completer(complete)
            readline.set_completer_delims(" \t\n")


@click.command("tui", short_help="The HighhX console: run tasks with a live dashboard, inspect traces.")
@pass_app
def tui(app: App) -> int:
    """An interactive console for computer-use work: type a goal to run it (agent loop with a live
    dashboard of plan, action, computer state, grounding, verification, recovery, cost and
    trace), or a /command — /help lists them, / and Tab open the palette. Approvals are asked
    here. Ctrl-C cancels a running task (resumable with /resume), Ctrl-D leaves."""
    if not app.options.is_interactive():
        raise click.UsageError("The console needs an interactive terminal; use the CLI commands directly.")
    return HighhxConsole(app).run()
