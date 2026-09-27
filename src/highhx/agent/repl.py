"""The interactive HighhX session: one experience for HighhX Free and HighhX Pro.

    request ─┬─ /command     slash commands (session control)
             ├─ !command     a shell command through the engine (risk, policy, approval, history)
             ├─ highhx …     any HighhX command, run in this session
             └─ plain text ─┬─ AI agent available (Pro)  → the agent session: plan → tools → approvals → result
                            └─ otherwise (Free)          → a deterministic intent (no AI), or an explanation of
                                                           the capability it needs with local alternatives

What the session may do comes from :mod:`highhx.cloud.capabilities` (the account's
platform-reported plan features plus local capabilities); the UI is the same either way.
"""

from __future__ import annotations

import contextlib
import dataclasses
import shlex
import signal
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import FrameType
from typing import TYPE_CHECKING

import click
from rich.markup import escape
from rich.table import Table

from highhx.agent.input import (
    CONTINUATION,
    CONTINUATION_MARKUP,
    PROMPT,
    PROMPT_MARKUP,
    ansi_prompt,
    read_request,
    setup_readline,
)
from highhx.agent.memory import ProjectMemory
from highhx.agent.model.registry import PROVIDER_NAMES, UPSTREAM_NAMES, provider_info
from highhx.agent.permissions import ApprovalMode
from highhx.agent.router import LocalAction, Route, route
from highhx.agent.session import AgentSession, TurnResult
from highhx.agent.ui import VIEW_PRO, TerminalUI, format_tokens
from highhx.cloud import capabilities
from highhx.cloud.capabilities import LABELS, LOCAL, Capability, Connection, Entitlements
from highhx.core.errors import (
    AccountError,
    CloudError,
    ExitCode,
    HighhXError,
    ModelProviderError,
    OperationCancelledError,
    PlanRequiredError,
    QuotaExceededError,
)
from highhx.execution.cancellation import CancellationToken

if TYPE_CHECKING:
    from highhx.agent.context import ProjectContext
    from highhx.cloud.account import Account, CloudAccount
    from highhx.commands import App

SessionFactory = Callable[[], "tuple[AgentSession, Account, bool]"]

# Kept as a module attribute: tests replace it to keep the real history file untouched.
_setup_readline = setup_readline

ACCOUNT_COMMANDS = frozenset({"login", "logout", "account"})
"""HighhX commands after which the session re-checks the account's capabilities."""
AGENT_SUBCOMMANDS_OK = frozenset({"sessions", "models", "stop", "--help", "-h"})
STRAY_ANSWERS = frozenset({"y", "yes", "n", "no", "a", "always", "p"})
"""Answers typed when no question is open (a prompt already answered); never sent as a request."""


@dataclass(frozen=True)
class SlashCommand:
    name: str
    usage: str
    help: str
    agent: bool = False
    """Needs the AI agent (HighhX Pro)."""


SLASH_COMMANDS = (
    SlashCommand("help", "/help", "Show commands and tips"),
    SlashCommand("status", "/status", "Project, plan, account and session at a glance"),
    SlashCommand("tools", "/tools", "What this session can do (local and Pro capabilities)"),
    SlashCommand("plan", "/plan", "Show the current plan and its progress", agent=True),
    SlashCommand("context", "/context", "What the session knows: project facts, memory, tools"),
    SlashCommand("model", "/model [provider|model]", "Show or switch the AI provider / model", agent=True),
    SlashCommand("mode", "/mode [ask|auto-edit|read-only]", "Show or change the approval mode", agent=True),
    SlashCommand("config", "/config", "The effective project configuration"),
    SlashCommand("account", "/account", "Your HighhX account and plan"),
    SlashCommand("usage", "/usage", "AI usage this billing period"),
    SlashCommand("history", "/history", "Recent agent sessions (Pro) or HighhX operations"),
    SlashCommand("changes", "/changes", "Files the agent changed in this session", agent=True),
    SlashCommand("undo", "/undo", "Revert the files changed in the last turn", agent=True),
    SlashCommand("memory", "/memory [clear]", "Show (or clear) the project memory"),
    SlashCommand("pro", "/pro", "What HighhX Pro adds, and how to get it"),
    SlashCommand("clear", "/clear", "Start a fresh conversation (keeps memory and settings)"),
    SlashCommand("quit", "/quit", "Exit (also /exit, Ctrl+D)"),
)


@contextlib.contextmanager
def turn_interrupts(token: CancellationToken) -> Iterator[None]:
    """Ctrl+C cancels the running request, not the whole session."""
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


@contextlib.contextmanager
def prompt_interrupts() -> Iterator[None]:
    """At the prompt Ctrl+C only raises KeyboardInterrupt (it must not cancel the application's token)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def nested_session(argv: list[str]) -> bool:
    """``highhx`` / ``highhx agent …`` inside the session would start a second session."""
    if not argv:
        return True
    return argv[0] == "agent" and (len(argv) == 1 or argv[1] not in AGENT_SUBCOMMANDS_OK)


class AgentREPL:
    """The interactive session. ``session`` is the AI agent session, or ``None`` without the agent."""

    def __init__(
        self,
        session: AgentSession | None,
        ui: TerminalUI,
        cloud: CloudAccount | None,
        account: Account | None = None,
        *,
        app: App | None = None,
        entitlements: Entitlements | None = None,
        session_factory: SessionFactory | None = None,
        read_line: Callable[[str], str] | None = None,
    ) -> None:
        if session is None and app is None:
            raise ValueError("an app is required without an agent session")
        self.session = session
        self.app: App = session.app if session is not None else app  # type: ignore[assignment]
        self.ui = ui
        self.cloud = cloud
        self.account = account
        if entitlements is None:
            entitlements = capabilities.from_account(account) if account is not None else capabilities.local()
        self.entitlements = entitlements
        self.session_factory = session_factory
        self._custom_reader = read_line is not None
        self._read_line = read_line or self._terminal_input
        self._running = True
        self._token: CancellationToken | None = None
        self._context: ProjectContext | None = None

    # ---------------------------------------------------------------- state
    @property
    def context(self) -> ProjectContext:
        if self.session is not None:
            return self.session.context
        if self._context is None:
            from highhx.agent.context import gather

            self._context = gather(self.app)
        return self._context

    @property
    def agent_ready(self) -> bool:
        return self.session is not None

    # ------------------------------------------------------------------ banner
    def git_state(self) -> tuple[str | None, bool, int]:
        """``(branch, clean, changes)`` now (the session's context is a start-up snapshot)."""
        try:
            repo = self.app.git_repo
            if repo.is_repo():
                status = repo.status()
                return status.branch, status.clean, status.change_count
        except HighhXError:
            pass
        return None, True, 0

    def git_line(self) -> str:
        branch, clean, changes = self.git_state()
        if branch is None:
            return "not a git repository"
        if clean:
            return f"{branch} • clean"
        return f"{branch} • {changes} uncommitted change{'s' if changes != 1 else ''}"

    def banner_rows(self) -> list[tuple[str, str]]:
        ctx = self.context
        rows = [("Project", f"[bold]{escape(ctx.name)}[/bold] [dim]{escape(ctx.stack_label)}[/dim]")]
        if self.session is not None:
            ai = f"[ok]Connected[/ok] [dim]{escape(self.provider_label())}[/dim]"
            if self.entitlements.connection == Connection.CACHED:
                ai = f"[warn]Offline account cache[/warn] [dim]{escape(self.provider_label())}[/dim]"
            rows.append(("AI", ai))
            if self.session.settings.approval != ApprovalMode.ASK:
                rows.append(("Mode", f"[warn]{self.session.settings.approval}[/warn]"))
        else:
            rows.append(("AI", "[dim]not available — local commands and known requests only (/pro)[/dim]"))
        if not ctx.initialized:
            rows.append(("Setup", "[warn]not initialized[/warn] [dim]— `highhx init` adds workflows & history[/dim]"))
        if self.app.options.dry_run:
            rows.append(("Dry run", "[warn]on[/warn] [dim]— commands are previewed, not executed[/dim]"))
        return rows

    def status_lines(self) -> list[tuple[str, str]]:
        line = self.git_line()
        git_style = "yellow" if "uncommitted" in line else "dim"
        plan_style = "bold magenta" if self.entitlements.tier == "Pro" else "bold"
        if self.entitlements.connection in (Connection.CACHED, Connection.UNAVAILABLE):
            plan_style = "yellow"
        return [(self.git_line(), git_style), (self.entitlements.status, plan_style)]

    def provider_label(self) -> str:
        if self.session is None:
            return "none"
        choice = self.session.settings.provider
        model = self.session.settings.model
        if self.session.provider.name not in PROVIDER_NAMES:  # registered by a plugin
            return f"{self.session.provider.name} · {model or self.session.provider.default_model}"
        if choice == "highhx" or choice not in PROVIDER_NAMES:
            return "HighhX (managed)" + (f" · {model}" if model else "")
        info = provider_info(choice)
        return f"{info.label} · {model or info.default_model}"

    def show_banner(self, *, resumed: bool = False) -> None:
        self.ui.banner(self.context, self.banner_rows(), status=self.status_lines())
        if self.entitlements.problem:
            self.ui.notice("warn", self.entitlements.problem)
        if resumed and self.session is not None:
            record = self.session.record
            title = record.title if record else ""
            self.ui.print(
                f"[dim]Resumed session[/dim] [bold]{escape(title)}[/bold] [dim]({self.session.turns} turns)[/dim]\n"
            )
        if self.session is not None:
            self.ui.print("What would you like me to do? [dim](/help for commands)[/dim]")
        else:
            self.ui.print(
                "What would you like to do? [dim]Try “run the tests”, “show git status”, `!ls` or /help.[/dim]"
            )

    # --------------------------------------------------------------------- run
    def _on_terminate(self, _signum: int, _frame: FrameType | None) -> None:
        """`highhx agent stop` / SIGTERM: cancel the running request and end the session."""
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
                    self.ui.print(f"\n[bold magenta]{PROMPT}[/bold magenta]{escape(text)}")
                else:
                    try:
                        with prompt_interrupts():
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
                self.handle(text)
        finally:
            save_history()
            if self.session is not None:
                self.session.close()
                self.ui.print("\n[dim]Session saved. Resume with `highhx agent --continue`.[/dim]")
            else:
                self.ui.print("\n[dim]Bye.[/dim]")
        return 0

    def _terminal_input(self, prompt: str) -> str:
        """Read one line with readline, using a prompt whose colour codes readline can measure."""
        color = self.ui.console.is_terminal and not self.ui.console.no_color
        text = PROMPT if prompt == PROMPT_MARKUP else CONTINUATION
        styled = ansi_prompt(text, "1;35" if text == PROMPT else "2", color=color)
        self.ui.console.file.flush()
        return input(styled)

    def _read(self) -> str:
        self.ui.print()
        return read_request(self._read_line, prompt=PROMPT_MARKUP, continuation=CONTINUATION_MARKUP)

    def handle(self, text: str) -> None:
        """Dispatch one request (see the module docstring)."""
        text = text.strip()
        if not text:
            return
        if text.startswith("/"):
            self.slash(text)
        elif text.startswith("!"):
            self.shell(text[1:].strip())
        elif text == "highhx" or text.startswith("highhx "):
            try:
                argv = shlex.split(text)[1:]
            except ValueError as exc:
                self.ui.notice("warn", f"Could not parse the command: {exc}")
                return
            self.command(argv)
        elif text.lower() in STRAY_ANSWERS:
            self.ui.notice("info", "Nothing is waiting for an answer — describe what you want, or /help.")
        elif self.session is not None:
            self.turn(text)
        else:
            self.local_request(text)

    # ----------------------------------------------------------- AI agent turn
    def turn(self, text: str) -> TurnResult | None:
        assert self.session is not None
        token = CancellationToken()
        self._token = token
        self.ui.print()
        try:
            with turn_interrupts(token):
                result = self.session.run_turn(text, cancel=token)
        except (PlanRequiredError, AccountError, QuotaExceededError) as exc:
            self.ui.assistant_finished()
            self.ui.notice("error", exc.message)
            if exc.hint:
                self.ui.print(f"  [dim]{escape(exc.hint)}[/dim]")
            self.refresh_entitlements()
            if self.session is None:
                self.local_request(text)
            return None
        except (ModelProviderError, CloudError) as exc:
            self.ui.assistant_finished()
            self.ui.notice("error", exc.message)
            if exc.hint:
                self.ui.print(f"  [dim]{escape(exc.hint)}[/dim]")
            unreachable = exc.connection if isinstance(exc, ModelProviderError) else exc.status is None
            self.platform_fallback(text, unreachable=unreachable)
            return None
        except HighhXError as exc:
            self.ui.assistant_finished()
            self.ui.notice("error", exc.message)
            if exc.hint:
                self.ui.print(f"  [dim]{escape(exc.hint)}[/dim]")
            return None
        self._platform_reachable(True)
        if result.stopped == "cancelled":
            self.ui.assistant_finished()
            self.ui.notice("warn", "Interrupted. The conversation is kept — tell me how to continue.")
        self.footer(result)
        return result

    def _platform_reachable(self, reachable: bool) -> None:
        """Track the platform connection for the status line (capabilities are unchanged: the
        agent stays attached and the next request tries the platform again)."""
        connection = self.entitlements.connection
        if not reachable and connection in (Connection.CONNECTED, Connection.CACHED):
            self.entitlements = dataclasses.replace(self.entitlements, connection=Connection.UNAVAILABLE)
        elif reachable and connection == Connection.UNAVAILABLE and self.entitlements.account is not None:
            cached = self.entitlements.account.cached
            self.entitlements = dataclasses.replace(
                self.entitlements, connection=Connection.CACHED if cached else Connection.CONNECTED, problem=None
            )

    def platform_fallback(self, text: str, *, unreachable: bool = True) -> None:
        """The AI request failed: say why, and offer the deterministic local route for this request."""
        if unreachable:
            self._platform_reachable(False)
            self.ui.notice("warn", "HighhX platform unavailable. Local capabilities remain available.")
        else:
            self.ui.notice("warn", "The AI request failed. Local capabilities remain available.")
        planned = route(text)
        if planned.intent is not None and self.ui.ask(
            f"Run {planned.intent.description} locally instead (no AI)?", default=True
        ):
            self.run_intent(planned)

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

    # ------------------------------------------------------------- local route
    def local_request(self, text: str) -> None:
        """Without the AI agent: a deterministic intent, or the capability the request needs."""
        planned = route(text)
        if planned.intent is not None:
            self.run_intent(planned)
            return
        assert planned.capability is not None
        self.offer(planned.reason, planned.alternatives)

    def offer(self, reason: str, alternatives: tuple[LocalAction, ...]) -> None:
        choice = self.ui.capability_panel(reason, alternatives)
        if choice == VIEW_PRO:
            self.show_pro()
        elif isinstance(choice, LocalAction):
            self.command(list(choice.argv))

    def run_intent(self, planned: Route) -> int:
        assert planned.intent is not None
        intent = planned.intent
        if intent.kind == "command":
            return self.command(list(intent.argv), label=intent.description)
        from highhx.commands.computer.do import execute_intent

        return self._local(intent.description, lambda: execute_intent(self.app, intent), detail="local · no AI")

    def shell(self, command: str) -> int:
        """``!command``: through `highhx exec` — risk classification, policy, approval, history."""
        if not command:
            self.ui.notice("info", "Usage: !<command>, e.g. !ls -la or !git log --oneline -5")
            return int(ExitCode.USAGE)
        return self.command(["exec", "--shell", command], label=command)

    def command(self, argv: list[str], *, label: str | None = None) -> int:
        """Run a HighhX command in this session (the same code path as on the command line)."""
        if nested_session(argv):
            self.ui.notice("info", "You are already in the HighhX session — just type your request.")
            return 0
        shown = label or "highhx " + " ".join(argv)
        code = self._local(shown, lambda: self._invoke(argv))
        if argv[0] in ACCOUNT_COMMANDS:
            self.refresh_entitlements()
        return code

    def _invoke(self, argv: list[str]) -> int:
        from highhx.cli import _emit_error, cli

        options = dataclasses.replace(self.app.options)
        try:
            result = cli.main(args=list(argv), prog_name="highhx", standalone_mode=False, obj=self.app)
            return int(result) if isinstance(result, int) and not isinstance(result, bool) else 0
        except click.exceptions.Exit as exc:
            return int(exc.exit_code)
        except click.exceptions.Abort:
            return int(ExitCode.CANCELLED)
        except click.ClickException as exc:
            exc.show()
            return int(exc.exit_code)
        except (KeyboardInterrupt, OperationCancelledError):
            raise
        except HighhXError as exc:
            _emit_error(self.app, exc)
            return int(exc.exit_code)
        finally:
            # Flags given to one command (`highhx status --json`) do not stick to the session.
            for item in dataclasses.fields(options):
                setattr(self.app.options, item.name, getattr(options, item.name))

    def _local(self, label: str, action: Callable[[], int], *, detail: str = "") -> int:
        token = CancellationToken()
        self._token = token
        self.app.ctx.cancel = token
        self.ui.activity_started(label, detail=detail)
        started = time.monotonic()
        code: int
        try:
            with self.local_io(), turn_interrupts(token):
                code = action()
        except (KeyboardInterrupt, OperationCancelledError):
            code = int(ExitCode.CANCELLED)
        except HighhXError as exc:
            self.ui.notice("error", exc.message)
            if exc.hint:
                self.ui.print(f"  [dim]{escape(exc.hint)}[/dim]")
            code = int(exc.exit_code)
        self.ui.activity_finished(label, code, time.monotonic() - started)
        return code

    @contextlib.contextmanager
    def local_io(self) -> Iterator[None]:
        """Local commands print and prompt on the terminal directly, even while an agent session
        (which routes the engine's output and prompts into its own UI) is attached."""
        engine = self.app.engine
        approvals = self.app.approvals
        saved = engine.output, approvals.prompter
        engine.output, approvals.prompter = self.app.output, self.app.prompter
        try:
            yield
        finally:
            engine.output, approvals.prompter = saved

    # ---------------------------------------------------------- entitlements
    def refresh_entitlements(self) -> None:
        """Re-check the account (after login / logout / upgrade, or a plan error) and adapt in place."""
        if self.cloud is None:
            return
        before = self.entitlements
        self.entitlements = capabilities.resolve(self.cloud)
        if self.entitlements.has(Capability.AI_AGENT) and self.session is None and self.session_factory is not None:
            try:
                self.session, self.account, _ = self.session_factory()
            except HighhXError as exc:
                self.ui.notice("warn", f"The AI agent could not start: {exc.message}")
                self.entitlements = dataclasses.replace(self.entitlements, capabilities=LOCAL, problem=exc.message)
                return
            self.ui.notice("info", f"{self.entitlements.status} — the AI agent now handles your requests.")
        elif not self.entitlements.has(Capability.AI_AGENT) and self.session is not None:
            self.session.close()
            self.session = None
            self.ui.notice("info", f"{self.entitlements.status} — continuing with local capabilities.")
        elif before.status != self.entitlements.status:
            self.ui.notice("info", self.entitlements.status)

    def show_pro(self) -> None:
        from highhx.commands.cloud.account import render_plans

        self.ui.print()
        render_plans(self.ui.console, self.entitlements.account.plan.id if self.entitlements.account else None)
        self.ui.print()
        if not self.entitlements.signed_in:
            self.ui.print(
                "[bold]Get started:[/bold] `highhx login` (creates your account), then `highhx account upgrade`."
            )
        elif self.entitlements.tier == "Pro":
            self.ui.print("[ok]You are on HighhX Pro.[/ok]")
        else:
            self.ui.print("[bold]Upgrade:[/bold] `highhx account upgrade` — everything local keeps working either way.")
        self.ui.print(
            "[dim]Run those here too: type `highhx login`. The session switches over when your plan does.[/dim]"
        )

    # ------------------------------------------------------------------ slash
    def slash(self, text: str) -> None:
        name, _, arg = text[1:].partition(" ")
        name, arg = name.strip().lower(), arg.strip()
        if name in ("exit", "q"):
            name = "quit"
        spec = next((c for c in SLASH_COMMANDS if c.name == name), None)
        handler = getattr(self, f"cmd_{name.replace('-', '_')}", None) if spec else None
        if handler is None:
            known = ", ".join(f"/{c.name}" for c in SLASH_COMMANDS)
            self.ui.notice("warn", f"Unknown command /{name}. Try: {known}")
            return
        if spec is not None and spec.agent and self.session is None:
            self.offer(f"/{name} is part of the AI agent, which requires HighhX Pro.", ())
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
        table.add_column(style="dim")
        for command in SLASH_COMMANDS:
            table.add_row(escape(command.usage), escape(command.help), "Pro" if command.agent else "")
        if self.session is not None:
            self.ui.print(
                "\n[bold]Just describe what you want[/bold] — e.g. [italic]fix my failing tests[/italic], "
                "[italic]explain how this project works[/italic], [italic]prepare this project for release[/italic].\n"
            )
        else:
            self.ui.print(
                "\n[bold]Describe what you want.[/bold] Known requests run locally without AI — "
                "[italic]run the tests[/italic], [italic]run the checks[/italic], [italic]build[/italic], "
                "[italic]show git status[/italic], [italic]security scan[/italic], [italic]open localhost:3000[/italic]. "
                "Open-ended requests use the AI agent (HighhX Pro).\n"
            )
        self.ui.print(table)
        self.ui.print(
            "\n[dim]!<command> runs a shell command (risk-checked, approval-gated); `highhx <command>` runs any "
            'HighhX command. End a line with \\ or wrap text in """ for multi-line input. '
            "Ctrl+C interrupts the current request; Ctrl+D exits.[/dim]"
        )

    def cmd_quit(self, _arg: str) -> None:
        self._running = False

    def cmd_clear(self, _arg: str) -> None:
        if self.ui.console.is_terminal:
            self.ui.console.clear()
            self.show_banner()
        if self.session is not None:
            self.session.clear()
            self.ui.notice("info", "Conversation cleared. Project memory and settings are kept.")

    def cmd_status(self, _arg: str) -> None:
        rows = [("Plan", escape(self.entitlements.status)), ("Git", escape(self.git_line())), *self.banner_rows()]
        account = self.entitlements.account or self.account
        if account is not None:
            rows.append(("Account", f"{escape(account.email)} · {escape(account.plan.name)}"))
        elif not self.entitlements.signed_in:
            rows.append(("Account", "[dim]not signed in — `highhx login` for HighhX Pro[/dim]"))
        s = self.session
        if s is not None:
            record = s.record
            rows += [
                ("Session", escape(record.id if record else "not saved")),
                ("Turns", str(s.turns)),
                ("Tokens", f"{format_tokens(s.usage.input_tokens)} in · {format_tokens(s.usage.output_tokens)} out"),
                (
                    "Approvals",
                    f"{s.settings.approval}"
                    + (f" · always-allowed: {', '.join(sorted(s.permissions.grants))}" if s.permissions.grants else ""),
                ),
            ]
        self._grid(rows)

    def _grid(self, rows: list[tuple[str, str]]) -> None:
        table = Table.grid(padding=(0, 3))
        table.add_column(style="dim", no_wrap=True)
        table.add_column(overflow="fold")
        for key, value in rows:
            table.add_row(key, value)
        self.ui.print(table)

    def cmd_tools(self, _arg: str) -> None:
        table = Table(box=None, header_style="dim", pad_edge=False)
        table.add_column("capability")
        table.add_column("")
        for capability in Capability:
            available = self.entitlements.has(capability)
            mark = "[ok]available[/ok]" if available else "[dim]HighhX Pro[/dim]"
            table.add_row(escape(LABELS[capability]), mark)
        self.ui.print(table)
        if self.session is not None:
            names = self.session.registry.names()
            self.ui.print(f"\n[dim]Agent tools ({len(names)}):[/dim] {escape(', '.join(names))}")
        else:
            self.ui.print(
                "\n[dim]Local tools run without AI: every `highhx` command, !shell commands and known requests. "
                "Platform capabilities are granted by your HighhX account (/account).[/dim]"
            )

    def cmd_plan(self, _arg: str) -> None:
        assert self.session is not None
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
        ctx = self.context
        memory = s.memory if s is not None else self._memory()
        rows = [
            ("Root", escape(str(ctx.root))),
            ("Stack", escape(ctx.stack_label)),
            ("Commands", escape(", ".join(f"{k}={v}" for k, v in sorted(ctx.commands.items())) or "none detected")),
            ("Files indexed", str(len(ctx.tree))),
            ("Instructions", escape(", ".join(n for n, _ in ctx.instructions) or "none (add HIGHHX.md or AGENTS.md)")),
            ("Memory", f"{len(memory.facts())} fact(s) in {escape(str(memory.path))}"),
        ]
        if s is not None:
            rows += [
                ("Tools", f"{len(s.registry)} available"),
                ("Conversation", f"{len(s.messages)} messages · system prompt {len(s.system):,} chars"),
            ]
        else:
            rows.append(("Tools", "local commands, !shell and known requests (no AI)"))
        self._grid(rows)

    def _memory(self) -> ProjectMemory:
        return ProjectMemory.for_project(self.app.root, initialized=self.app.initialized, redactor=self.app.redactor)

    def cmd_model(self, arg: str) -> None:
        from highhx.agent.bootstrap import build_provider

        s = self.session
        assert s is not None and self.cloud is not None and self.account is not None
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
        assert self.session is not None
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

    def cmd_config(self, _arg: str) -> None:
        self.command(["config", "show"])

    def cmd_account(self, _arg: str) -> None:
        self.command(["account", "status"])

    def cmd_pro(self, _arg: str) -> None:
        self.show_pro()

    def cmd_history(self, _arg: str) -> None:
        if self.session is None:
            self.command(["history"])
            return
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
        assert self.session is not None
        paths = self.session.journal.changed_paths()
        if not paths:
            self.ui.notice("info", "No files changed in this session.")
            return
        for path in paths:
            state = "deleted" if not path.exists() else "modified"
            self.ui.print(f"  [dim]{state:<9}[/dim] {escape(self.session.permissions.relative(path))}")

    def cmd_undo(self, _arg: str) -> None:
        assert self.session is not None
        restored = self.session.undo()
        if not restored:
            self.ui.notice("info", "Nothing to undo.")
            return
        self.ui.notice("info", f"Restored {len(restored)} file(s): {', '.join(restored)}")
        self.session.note(f"The user undid your last changes to: {', '.join(restored)}. Those edits are reverted.")

    def cmd_memory(self, arg: str) -> None:
        memory = self.session.memory if self.session is not None else self._memory()
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

        if self.cloud is None or not self.entitlements.signed_in:
            self.ui.notice("info", "AI usage is metered for HighhX accounts — sign in with `highhx login`.")
            return
        render_usage(self.ui.console, self.cloud.usage())
