"""Interactive agent loop: natural language by default, slash commands for control."""

from __future__ import annotations

import contextlib
import signal
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import FrameType
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.table import Table

from highhx.agent.model.registry import PROVIDER_NAMES, UPSTREAM_NAMES, provider_info
from highhx.agent.permissions import ApprovalMode
from highhx.agent.session import AgentSession, TurnResult
from highhx.agent.ui import TerminalUI, format_tokens
from highhx.core.errors import HighhXError
from highhx.execution.cancellation import CancellationToken
from highhx.utils.paths import user_data_dir

if TYPE_CHECKING:
    from highhx.cloud.account import Account, CloudAccount

HISTORY_FILE = "agent-input-history"


@dataclass(frozen=True)
class SlashCommand:
    name: str
    usage: str
    help: str


SLASH_COMMANDS = (
    SlashCommand("help", "/help", "Show commands and tips"),
    SlashCommand("status", "/status", "Project, session, model and account at a glance"),
    SlashCommand("plan", "/plan", "Show the current plan and its progress"),
    SlashCommand("context", "/context", "What the agent knows: project facts, memory, tools, tokens"),
    SlashCommand("model", "/model [provider|model]", "Show or switch the AI provider / model"),
    SlashCommand("mode", "/mode [ask|auto-edit|read-only]", "Show or change the approval mode"),
    SlashCommand("history", "/history", "Recent agent sessions for this project"),
    SlashCommand("changes", "/changes", "Files the agent changed in this session"),
    SlashCommand("undo", "/undo", "Revert the files changed in the last turn"),
    SlashCommand("memory", "/memory [clear]", "Show (or clear) the project memory"),
    SlashCommand("usage", "/usage", "AI usage this billing period"),
    SlashCommand("clear", "/clear", "Start a fresh conversation (keeps memory and settings)"),
    SlashCommand("quit", "/quit", "Exit (also /exit, Ctrl+D)"),
)


@contextlib.contextmanager
def turn_interrupts(token: CancellationToken) -> Iterator[None]:
    """Ctrl+C cancels the running turn, not the whole session."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(_signum: int, _frame: FrameType | None) -> None:
        token.cancel("interrupted")
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _setup_readline() -> Callable[[], None]:
    try:
        import readline
    except ImportError:  # pragma: no cover - Windows without pyreadline
        return lambda: None
    path = user_data_dir() / HISTORY_FILE
    with contextlib.suppress(OSError):
        readline.read_history_file(str(path))
    readline.set_history_length(1000)

    def save() -> None:
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            readline.write_history_file(str(path))

    return save


class AgentREPL:
    def __init__(
        self,
        session: AgentSession,
        ui: TerminalUI,
        cloud: CloudAccount,
        account: Account,
        *,
        read_line: Callable[[str], str] | None = None,
    ) -> None:
        self.session = session
        self.ui = ui
        self.cloud = cloud
        self.account = account
        self._read_line = read_line or ui.console.input
        self._running = True
        self._token: CancellationToken | None = None

    # ------------------------------------------------------------------ banner
    def banner_rows(self) -> list[tuple[str, str]]:
        ctx = self.session.context
        git = "-" if ctx.branch is None else escape(ctx.branch)
        state = (
            "[dim]not a git repository[/dim]"
            if ctx.branch is None
            else ("[ok]clean[/ok]" if ctx.clean else f"[warn]{escape(ctx.git_label)}[/warn]")
        )
        ai = f"[ok]Connected[/ok] [dim]{escape(self.provider_label())}[/dim]"
        if self.account.cached:
            ai = f"[warn]Offline account cache[/warn] [dim]{escape(self.provider_label())}[/dim]"
        rows = [
            ("Project", f"[bold]{escape(ctx.name)}[/bold]"),
            ("Stack", escape(ctx.stack_label)),
            ("Branch", git),
            ("Status", state),
            ("AI", ai),
        ]
        if not ctx.initialized:
            rows.append(
                ("Setup", "[warn]HighhX not initialized[/warn] [dim]— `highhx init` adds workflows & history[/dim]")
            )
        if self.session.settings.approval != ApprovalMode.ASK:
            rows.append(("Mode", f"[warn]{self.session.settings.approval}[/warn]"))
        return rows

    def provider_label(self) -> str:
        choice = self.session.settings.provider
        model = self.session.settings.model
        if self.session.provider.name not in PROVIDER_NAMES:  # registered by a plugin
            return f"{self.session.provider.name} · {model or self.session.provider.default_model}"
        if choice == "highhx" or choice not in PROVIDER_NAMES:
            return "HighhX (managed)" + (f" · {model}" if model else "")
        info = provider_info(choice)
        return f"{info.label} · {model or info.default_model}"

    def show_banner(self, *, resumed: bool = False) -> None:
        self.ui.banner(self.session.context, self.banner_rows())
        if resumed:
            record = self.session.record
            title = record.title if record else ""
            self.ui.print(
                f"[dim]Resumed session[/dim] [bold]{escape(title)}[/bold] [dim]({self.session.turns} turns)[/dim]\n"
            )
        self.ui.print("What would you like me to do? [dim](/help for commands)[/dim]")

    # --------------------------------------------------------------------- run
    def _on_terminate(self, _signum: int, _frame: FrameType | None) -> None:
        """`highhx agent stop` / SIGTERM: cancel the running turn and end the session."""
        self._running = False
        if self._token is not None:
            self._token.cancel("stopped")
        raise KeyboardInterrupt

    def run(self, first: str | None = None, *, resumed: bool = False) -> int:
        previous = None
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, self._on_terminate)
        try:
            return self._run(first, resumed=resumed)
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)

    def _run(self, first: str | None = None, *, resumed: bool = False) -> int:
        save_history = _setup_readline()
        self.show_banner(resumed=resumed)
        pending = first
        interrupts = 0
        try:
            while self._running:
                if pending is not None:
                    text, pending = pending, None
                    self.ui.print(f"\n[bold magenta]❯[/bold magenta] {escape(text)}")  # noqa: RUF001
                else:
                    try:
                        text = self._read()
                    except EOFError:
                        break
                    except KeyboardInterrupt:
                        interrupts += 1
                        if interrupts >= 2 or not self._running:
                            break
                        self.ui.print("\n[dim]Press Ctrl+C again or type /quit to exit.[/dim]")
                        continue
                interrupts = 0
                text = text.strip()
                if not text:
                    continue
                if text.startswith("/"):
                    self.slash(text)
                    continue
                self.turn(text)
        finally:
            save_history()
            self.session.close()
            self.ui.print("\n[dim]Session saved. Resume with `highhx agent --continue`.[/dim]")
        return 0

    def _read(self) -> str:
        self.ui.print()
        lines = [self._read_line("[bold magenta]❯[/bold magenta] ")]  # noqa: RUF001
        while lines[-1].endswith("\\"):
            lines[-1] = lines[-1][:-1]
            lines.append(self._read_line("[dim]…[/dim] "))
        return "\n".join(lines)

    def turn(self, text: str) -> TurnResult | None:
        token = CancellationToken()
        self._token = token
        self.ui.print()
        try:
            with turn_interrupts(token):
                result = self.session.run_turn(text, cancel=token)
        except HighhXError as exc:
            self.ui.assistant_finished()
            self.ui.notice("error", exc.message)
            if exc.hint:
                self.ui.print(f"  [dim]{escape(exc.hint)}[/dim]")
            return None
        if result.stopped == "cancelled":
            self.ui.assistant_finished()
            self.ui.notice("warn", "Interrupted. The conversation is kept — tell me how to continue.")
        self.footer(result)
        return result

    def footer(self, result: TurnResult) -> None:
        parts = []
        if result.steps:
            parts.append(f"{result.steps} step{'s' if result.steps != 1 else ''}")
        if result.changed_files:
            n = len(result.changed_files)
            parts.append(f"{n} file{'s' if n != 1 else ''} changed (/undo)")
        if result.usage.total:
            parts.append(f"{format_tokens(result.usage.total)} tokens")
        parts.append(f"{result.seconds:.0f}s")
        self.ui.print()
        self.ui.footer(parts)

    # ------------------------------------------------------------------ slash
    def slash(self, text: str) -> None:
        name, _, arg = text[1:].partition(" ")
        name, arg = name.strip().lower(), arg.strip()
        handler = getattr(self, f"cmd_{name.replace('-', '_')}", None)
        if name in ("exit", "q"):
            handler = self.cmd_quit
        if handler is None:
            known = ", ".join(f"/{c.name}" for c in SLASH_COMMANDS)
            self.ui.notice("warn", f"Unknown command /{name}. Try: {known}")
            return
        try:
            handler(arg)
        except HighhXError as exc:
            self.ui.notice("error", exc.message)
            if exc.hint:
                self.ui.print(f"  [dim]{escape(exc.hint)}[/dim]")

    def cmd_help(self, _arg: str) -> None:
        table = Table.grid(padding=(0, 3))
        table.add_column(style="bold magenta", no_wrap=True)
        table.add_column()
        for command in SLASH_COMMANDS:
            table.add_row(escape(command.usage), escape(command.help))
        self.ui.print(
            "\n[bold]Just describe what you want[/bold] — e.g. [italic]fix my failing tests[/italic], "
            "[italic]explain how this project works[/italic], [italic]prepare this project for release[/italic].\n"
        )
        self.ui.print(table)
        self.ui.print(
            "\n[dim]End a line with \\ for multi-line input. Ctrl+C interrupts the current task; "
            "risky actions always ask first.[/dim]"
        )

    def cmd_quit(self, _arg: str) -> None:
        self._running = False

    def cmd_exit(self, arg: str) -> None:
        self.cmd_quit(arg)

    def cmd_clear(self, _arg: str) -> None:
        self.session.clear()
        self.ui.notice("info", "Conversation cleared. Project memory and settings are kept.")

    def cmd_status(self, _arg: str) -> None:
        s = self.session
        rows = self.banner_rows()
        record = s.record
        rows += [
            ("Session", escape(record.id if record else "not saved")),
            ("Turns", str(s.turns)),
            ("Tokens", f"{format_tokens(s.usage.input_tokens)} in · {format_tokens(s.usage.output_tokens)} out"),
            ("Account", f"{escape(self.account.email)} · {escape(self.account.plan.name)}"),
            (
                "Approvals",
                f"{s.settings.approval}"
                + (f" · always-allowed: {', '.join(sorted(s.permissions.grants))}" if s.permissions.grants else ""),
            ),
        ]
        table = Table.grid(padding=(0, 3))
        table.add_column(style="dim", no_wrap=True)
        table.add_column()
        for key, value in rows:
            table.add_row(key, value)
        self.ui.print(table)

    def cmd_plan(self, _arg: str) -> None:
        plan = self.session.plan
        if plan is None:
            self.ui.notice("info", "No plan yet. Ask for multi-step work and I'll propose one first.")
            return
        counts = plan.counts()
        self.ui.print(f"\n[bold]Plan[/bold] [dim]— {escape(plan.goal)}[/dim]")
        self.ui.print(self.ui.plan_table(plan))
        self.ui.print(
            f"[dim]{counts['done']} done · {counts['failed']} failed · {counts['pending'] + counts['in_progress']} remaining[/dim]"
        )

    def cmd_context(self, _arg: str) -> None:
        s = self.session
        ctx = s.context
        table = Table.grid(padding=(0, 3))
        table.add_column(style="dim", no_wrap=True)
        table.add_column(overflow="fold")
        table.add_row("Root", escape(str(ctx.root)))
        table.add_row("Stack", escape(ctx.stack_label))
        table.add_row(
            "Commands", escape(", ".join(f"{k}={v}" for k, v in sorted(ctx.commands.items())) or "none detected")
        )
        table.add_row("Files indexed", str(len(ctx.tree)))
        table.add_row(
            "Instructions", escape(", ".join(n for n, _ in ctx.instructions) or "none (add HIGHHX.md or AGENTS.md)")
        )
        table.add_row("Memory", f"{len(s.memory.facts())} fact(s) in {escape(str(s.memory.path))}")
        table.add_row("Tools", f"{len(s.registry)} available")
        table.add_row("Conversation", f"{len(s.messages)} messages · system prompt {len(s.system):,} chars")
        self.ui.print(table)

    def cmd_model(self, arg: str) -> None:
        from highhx.agent.bootstrap import build_provider

        s = self.session
        if not arg:
            self.ui.print(f"Current: [bold]{escape(self.provider_label())}[/bold]\n")
            table = Table(box=None, show_header=True, header_style="dim", pad_edge=False)
            table.add_column("provider")
            table.add_column("models")
            for name in PROVIDER_NAMES:
                info = provider_info(name)
                table.add_row(name, escape(", ".join(info.models[:6])))
            self.ui.print(table)
            self.ui.print(
                "\n[dim]All providers are reached via the HighhX gateway (your plan and usage apply). "
                "Switch with /model <provider> or /model <model-name>; set a default with "
                "`highhx account settings --provider … --model …`.[/dim]"
            )
            return
        choice, model = s.settings.provider, None
        if arg in PROVIDER_NAMES:
            choice = arg
        else:
            model = arg
            owner = next((n for n in UPSTREAM_NAMES if arg in provider_info(n).models), None)
            if owner is None:
                self.ui.notice("warn", f"'{arg}' is not a known model; the HighhX gateway will validate it.")
            else:
                choice = owner
        settings = s.settings
        previous = (settings.provider, settings.model)
        settings.provider, settings.model = choice, model
        try:
            provider = build_provider(settings, self.cloud, self.account)
        except HighhXError:
            settings.provider, settings.model = previous
            raise
        s.set_provider(provider, model, choice=choice)
        self.ui.notice("info", f"Now using {self.provider_label()}.")

    def cmd_mode(self, arg: str) -> None:
        if not arg:
            self.ui.print(
                f"Approval mode: [bold]{self.session.settings.approval}[/bold]  "
                "[dim](ask · auto-edit · read-only)[/dim]"
            )
            return
        try:
            mode = ApprovalMode(arg)
        except ValueError:
            self.ui.notice("warn", "Choose one of: ask, auto-edit, read-only")
            return
        self.session.set_mode(mode)
        self.ui.notice("info", f"Approval mode is now {mode}.")

    def cmd_history(self, _arg: str) -> None:
        store = self.session.store
        if store is None:
            self.ui.notice("warn", "Session history is unavailable (storage could not be opened).")
            return
        records = store.list(root=str(self.session.app.root), limit=15)
        if not records:
            self.ui.notice("info", "No sessions yet.")
            return
        table = Table(box=None, header_style="dim", pad_edge=False)
        for column in ("id", "updated", "turns", "tokens", "title"):
            table.add_column(column)
        current = self.session.record.id if self.session.record else None
        for r in records:
            marker = " [magenta]●[/magenta]" if r.id == current else ""
            table.add_row(
                escape(r.id) + marker,
                r.updated_at.replace("T", " ")[:16],
                str(r.turns),
                format_tokens(r.usage.total),
                escape(r.title),
            )
        self.ui.print(table)
        self.ui.print("\n[dim]Resume one with `highhx agent --resume <id>`.[/dim]")

    def cmd_changes(self, _arg: str) -> None:
        paths = self.session.journal.changed_paths()
        if not paths:
            self.ui.notice("info", "No files changed in this session.")
            return
        for path in paths:
            state = "deleted" if not path.exists() else "modified"
            self.ui.print(f"  [dim]{state:<9}[/dim] {escape(self.session.permissions.relative(path))}")

    def cmd_undo(self, _arg: str) -> None:
        restored = self.session.undo()
        if not restored:
            self.ui.notice("info", "Nothing to undo.")
            return
        self.ui.notice("info", f"Restored {len(restored)} file(s): {', '.join(restored)}")
        self.session.note(f"The user undid your last changes to: {', '.join(restored)}. Those edits are reverted.")

    def cmd_memory(self, arg: str) -> None:
        memory = self.session.memory
        if arg == "clear":
            memory.clear()
            self.ui.notice("info", "Project memory cleared.")
            return
        facts = memory.facts()
        if not facts:
            self.ui.notice("info", f"No project memory yet ({memory.path}).")
            return
        for fact in facts:
            self.ui.print(f"  • {escape(fact)}")
        self.ui.print(f"\n[dim]{escape(str(memory.path))}[/dim]")

    def cmd_usage(self, _arg: str) -> None:
        from highhx.commands.cloud.account import render_usage

        render_usage(self.ui.console, self.cloud.usage())
