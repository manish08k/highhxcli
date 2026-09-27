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
import logging
import shlex
import signal
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import FrameType
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from rich.table import Table

from highhx.actions import events as ev
from highhx.actions.executor import ActionExecutor, Planned
from highhx.actions.policy import Approval
from highhx.actions.resolver import Resolution, ResolverContext
from highhx.actions.spec import ActionResult
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
from highhx.agent.router import PRO_LEAD, LocalAction, route
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
from highhx.decision.advanced import advanced_reasoning_available
from highhx.execution.cancellation import CancellationToken

if TYPE_CHECKING:
    from highhx.agent.context import ProjectContext
    from highhx.cloud.account import Account, CloudAccount
    from highhx.commands import App
    from highhx.decision.deterministic import Decision
    from highhx.observability.runs import RunTrace
    from highhx.voice.mode import VoiceMode

log = logging.getLogger(__name__)

SessionFactory = Callable[[], "tuple[AgentSession, Account, bool]"]

# Kept as a module attribute: tests replace it to keep the real history file untouched.
_setup_readline = setup_readline

ACCOUNT_COMMANDS = frozenset({"login", "logout", "account"})
"""HighhX commands after which the session re-checks the account's capabilities."""
AGENT_SUBCOMMANDS_OK = frozenset({"sessions", "models", "stop", "--help", "-h"})
RECONNECT_SECONDS = 20.0
STRAY_ANSWERS = frozenset({"y", "yes", "n", "no", "a", "always", "p"})
"""Answers typed when no question is open (a prompt already answered); never sent as a request."""


@dataclass(frozen=True)
class SlashCommand:
    name: str
    usage: str
    help: str
    agent: bool = False
    """Needs the AI agent (HighhX Pro)."""
    group: str = "Session"


SLASH_COMMANDS = (
    # Run: ask, preview, approve, try again
    SlashCommand("plan", "/plan <request>", "Preview the steps, their risk and approvals — runs nothing", group="Run"),
    SlashCommand("approve", "/approve", "Run the previewed plan exactly (critical steps still ask)", group="Run"),
    SlashCommand("deny", "/deny", "Discard the previewed plan", group="Run"),
    SlashCommand("retry", "/retry", "Run the last request that failed again", group="Run"),
    SlashCommand("run", "/run <action> [k=v …]", "Run one catalog action, e.g. /run git.status", group="Run"),
    SlashCommand(
        "task",
        "/task <goal> [--verify test,check,build]",
        "Work until HighhX verifies it (tests, checks, build)",
        agent=True,
        group="Run",
    ),
    # Review: what happened
    SlashCommand("status", "/status", "Plan, project, pending work and this session at a glance", group="Review"),
    SlashCommand("history", "/history", "What this session ran, step by step", group="Review"),
    SlashCommand("changes", "/changes", "Files changed in this session", group="Review"),
    SlashCommand("undo", "/undo", "Revert the most recent file changes", group="Review"),
    SlashCommand("doctor", "/doctor", "Check tools, configuration, environment and ports", group="Review"),
    # Workflows
    SlashCommand("workflows", "/workflows", "Workflows in this project", group="Workflows"),
    SlashCommand(
        "workflow", "/workflow <sub> …", "create · list · run · inspect · runs · resume · cancel", group="Workflows"
    ),
    SlashCommand("resume", "/resume [run-id]", "Resume the last failed or cancelled workflow run", group="Workflows"),
    SlashCommand("cancel", "/cancel [run-id]", "Cancel a workflow running in another terminal", group="Workflows"),
    # Project
    SlashCommand("init", "/init", "Set up HighhX here: config, policies, workflows, history", group="Project"),
    SlashCommand("context", "/context", "What HighhX knows about this project", group="Project"),
    SlashCommand("tools", "/tools [category]", "Capabilities and the action catalog with risk levels", group="Project"),
    SlashCommand("config", "/config", "The effective project configuration", group="Project"),
    SlashCommand("memory", "/memory [clear]", "Project memory (facts kept across sessions)", group="Project"),
    # Account & AI
    SlashCommand("login", "/login", "Sign in to HighhX (creates your account)", group="Account & AI"),
    SlashCommand("account", "/account", "Your account and plan", group="Account & AI"),
    SlashCommand("pro", "/pro", "What HighhX Pro adds, and how to get it", group="Account & AI"),
    SlashCommand("usage", "/usage", "AI usage this billing period", group="Account & AI"),
    SlashCommand("model", "/model [name]", "Show or switch the AI model", agent=True, group="Account & AI"),
    SlashCommand(
        "mode", "/mode [ask|auto-edit|read-only]", "The agent's approval mode", agent=True, group="Account & AI"
    ),
    # Session
    SlashCommand("voice", "/voice [on|off|mute|status]", "Push-to-talk and spoken replies", group="Session"),
    SlashCommand("clear", "/clear", "Start a fresh conversation (memory and settings stay)", group="Session"),
    SlashCommand("help", "/help", "This list", group="Session"),
    SlashCommand("quit", "/quit", "Exit (also /exit, Ctrl+D)", group="Session"),
)
HELP_GROUPS = ("Run", "Review", "Workflows", "Project", "Account & AI", "Session")


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


OPTIONS_WITH_VALUES = frozenset({"-C", "--cwd", "--config-profile"})
"""Global options that take a value (skipped when looking for the command name)."""


def command_words(argv: list[str]) -> list[str]:
    """``argv`` without leading global options: ``["-v", "-C", "x", "agent", "fix"]`` → ``["agent", "fix"]``."""
    index = 0
    while index < len(argv) and argv[index].startswith("-"):
        if argv[index] in ("-h", "--help", "-V", "--version"):
            return argv[index:]
        index += 2 if argv[index] in OPTIONS_WITH_VALUES else 1
    return argv[index:]


def parse_inputs(text: str) -> dict[str, Any]:
    """``key=value …`` (values parsed as JSON when they are, else strings) or one JSON object."""
    import json

    text = text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from None
        if not isinstance(data, dict):
            raise ValueError("Inputs must be a JSON object.")
        return data
    inputs: dict[str, Any] = {}
    for token in shlex.split(text):
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"Expected key=value, got {token!r}.")
        try:
            inputs[key] = json.loads(value)
        except ValueError:
            inputs[key] = value
    return inputs


def nested_session(argv: list[str]) -> bool:
    """``highhx`` / ``highhx agent …`` inside the session would start a second session
    (global options such as ``-v`` or ``-C DIR`` in front do not change that)."""
    words = command_words(argv)
    if not words:
        return True
    return words[0] == "agent" and (len(words) == 1 or words[1] not in AGENT_SUBCOMMANDS_OK)


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
        session_id: str | None = None,
        first_run: bool = False,
        voice: bool = False,
        voice_mode: VoiceMode | None = None,
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
        self._last_check = time.monotonic()
        self._actions: ActionExecutor | None = None
        self.first_run = first_run
        self.last_task: Any = None
        self.stats = {"actions": 0, "failed": 0}
        self._voice_on_start = voice
        self._voice: VoiceMode | None = voice_mode
        self.last_summary = ""
        """One sentence about the last request's outcome (spoken in voice mode)."""
        self.pending: tuple[str, list[Planned]] | None = None
        """A plan previewed with /plan, waiting for /approve or /deny."""
        self.retryable: tuple[str, Any] | None = None
        """The last request that did not succeed: ("steps", [steps]) or ("turn", text)."""
        from highhx.utils.hashing import new_id

        record = session.record if session is not None else None
        self.session_id = session_id or (record.id if record is not None else f"s-{new_id()[:12]}")
        self._resolver_context: ResolverContext | None = None

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
            rows.append(
                ("AI", "[dim]not available on Free — deterministic automation · /pro shows what the agent adds[/dim]")
            )
        if not ctx.initialized:
            rows.append(
                ("Setup", "[warn]not set up yet[/warn] [dim]— /init adds config, policies, workflows & history[/dim]")
            )
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
        if self.first_run:
            self.welcome()
        if self.session is not None:
            self.ui.print("What would you like me to do? [dim](/help for commands)[/dim]")
        else:
            self.ui.print(
                "What would you like to do? HighhX automates your computer and your project — no AI needed:\n"
                "  [italic]run the tests[/italic] · [italic]show git status[/italic] · [italic]open Safari[/italic]"
                " · [italic]open YouTube and play …[/italic]\n"
                "  [italic]open Chrome and search for …[/italic] · [italic]create a folder called …[/italic]"
                " · [dim]`!ls` · /help[/dim]"
            )

    def welcome(self) -> None:
        """First session on this machine: the four steps from install to a first task."""
        steps = []
        if not self.context.initialized:
            steps.append("[bold]/init[/bold] sets up HighhX in this project (config, policies, workflows)")
        steps.append(
            "ask in plain words: [bold]run the tests[/bold], [bold]open YouTube and search for …[/bold], "
            "[bold]create a file called hello.py[/bold]"
        )
        steps.append("[bold]/plan <request>[/bold] previews what would run and its risk; [bold]/approve[/bold] runs it")
        if self.session is None:
            steps.append("[bold]/login[/bold] for HighhX Pro — the AI agent for open-ended tasks")
        else:
            steps.append("describe any task — the agent plans it, you approve changes, HighhX verifies them")
        from rich.panel import Panel

        body = "\n".join(f"[magenta]{n}.[/magenta] {step}" for n, step in enumerate(steps, start=1))
        self.ui.print(
            Panel(
                body,
                title="[bold magenta]Getting started[/bold magenta]",
                title_align="left",
                border_style="magenta",
                padding=(0, 2),
            )
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

    @property
    def voice(self) -> VoiceMode:
        if self._voice is None:
            from highhx.voice.mode import VoiceMode

            self._voice = VoiceMode(self.ui, self.app.ctx.events)
        return self._voice

    def _run(self, first: str | None = None, *, resumed: bool = False) -> int:
        save_history = _setup_readline()
        self.show_banner(resumed=resumed)
        if self._voice_on_start:
            self.voice.enable()
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
                        if self._voice is not None:
                            self._voice.interrupt()
                        interrupts += 1
                        if interrupts >= 2 or not self._running:
                            break
                        self.ui.print("\n[dim]Press Ctrl+C again or type /quit to exit.[/dim]")
                        continue
                interrupts = 0
                if self._voice is not None and self._voice.active and not text.strip():
                    heard = self.voice.listen(self._read_line)
                    if not heard:
                        continue
                    text = heard
                self.last_summary = ""
                self.handle(text)
                if self._voice is not None and self._voice.active and self.last_summary:
                    self._voice.speak(self.last_summary)
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
        """Dispatch one request (see the module docstring). Never raises: the session goes on."""
        try:
            self._handle(text)
        except KeyboardInterrupt:
            self.ui.assistant_finished()
            self.ui.notice("warn", "Interrupted.")
        except Exception as exc:
            self.unexpected(exc)

    def _handle(self, text: str) -> None:
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
            from highhx.agent.tasks import definition_of_done

            keys = definition_of_done(text)
            if keys:
                self.run_task(text, keys)  # the request states when it is done: verify it
            else:
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
            self.retryable = ("turn", text)
            self.platform_fallback(text, unreachable=unreachable)
            return None
        except HighhXError as exc:
            self.ui.assistant_finished()
            self.ui.notice("error", exc.message)
            if exc.hint:
                self.ui.print(f"  [dim]{escape(exc.hint)}[/dim]")
            return None
        self._platform_reachable(True)
        self.last_summary = result.text or ("Done." if result.stopped == "completed" else f"Stopped: {result.stopped}.")
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

    def run_task(self, goal: str, keys: list[str], *, attempts: int | None = None) -> Any:
        """An autonomous task: the agent works; HighhX verifies; failures go back; bounded attempts."""
        from highhx.agent.tasks import MAX_ATTEMPTS, TaskRunner, TaskSpec

        assert self.session is not None
        spec = TaskSpec.build(self.app, goal, keys, max_attempts=attempts or MAX_ATTEMPTS)
        self.ui.task_started(goal, [f"{c.name}" for c in spec.checks], spec.max_attempts, spec.missing)
        runner = TaskRunner(
            self.session,
            self.actions,
            on_attempt=self.ui.task_attempt,
            on_check=lambda c: self.ui.task_check(c.name, c.action, c.ok, c.summary),
            around_checks=self.local_io,
        )
        token = CancellationToken()
        self._token = token
        try:
            with turn_interrupts(token):
                report = runner.run(spec, cancel=token)
        except (PlanRequiredError, AccountError, QuotaExceededError, ModelProviderError, CloudError) as exc:
            self.ui.assistant_finished()
            self.ui.notice("error", exc.message)
            self.retryable = ("task", (goal, keys))
            return None
        self._platform_reachable(True)
        self.last_task = report
        self.ui.task_report(report)
        self.last_summary = {
            "verified": "Task verified.",
            "done": "Task done.",
            "unverified": "The task is not verified; checks are still failing.",
        }.get(report.status, f"Task {report.status}.")
        self.retryable = None if report.ok else ("task", (goal, keys))
        return report

    def platform_fallback(self, text: str, *, unreachable: bool = True) -> None:
        """The AI request failed: say why, and offer the deterministic local route for this request."""
        if unreachable:
            self._platform_reachable(False)
            self.ui.notice("warn", "HighhX platform unavailable. Local capabilities remain available.")
        else:
            self.ui.notice("warn", "The AI request failed. Local capabilities remain available.")
        planned = route(text, self.resolver_context())
        if planned.resolution is not None and self.ui.ask(
            f"Run {planned.resolution.description} locally instead (no AI)?", default=True
        ):
            self.run_resolution(planned.resolution)

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
        """Without the AI agent: the deterministic plan, or the capability the request needs."""
        decision = self.decide(text)
        planned = route(text, self.resolver_context(), decision=decision)
        if planned.resolution is None and self.reconnect() and self.session is not None:
            self.turn(text)  # the platform is back and grants the agent
            return
        if planned.resolution is not None:
            self.run_resolution(planned.resolution, decision)
            return
        assert planned.capability is not None
        self.trace(decision).finish()
        self.events.emit(ev.INTENT_UNRESOLVED, text=text, capability=str(planned.capability))
        if planned.unknown is not None:
            self.ui.unknown_action(planned.unknown.reason, planned.unknown.suggestions)
            self.last_summary = planned.unknown.reason
            return
        ent = self.entitlements
        if ent.connection == Connection.CACHED and ent.tier == "Pro":
            # A Pro plan (as last known) that cannot reach the platform: the gap is the connection.
            self.offer(
                "The AI agent needs the HighhX platform, which cannot be reached right now.",
                planned.alternatives,
                title="HighhX platform unavailable",
            )
            return
        self.offer(planned.reason, planned.alternatives, lead=PRO_LEAD)

    def decide(self, text: str) -> Decision:
        """The deterministic decision, with each step's risk from the executor's own classifier."""
        from highhx.decision.deterministic import DeterministicDecider

        return DeterministicDecider(
            self.resolver_context(), catalog=self.actions.catalog, risk_of=self._risk_of()
        ).decide(text)

    def trace(self, decision: Decision) -> RunTrace:
        from highhx.observability.runs import RunTrace

        return RunTrace(
            decision, db=self.app.db if self.app is not None else None, redactor=self._redactor(), source="session"
        )

    def _redactor(self) -> Any:
        return self.app.redactor if self.app is not None else None

    def offer(
        self,
        reason: str,
        alternatives: tuple[LocalAction, ...],
        *,
        title: str = "HighhX Pro capability",
        lead: str = "",
    ) -> None:
        self.last_summary = reason
        choice = self.ui.capability_panel(reason, alternatives, title=title, lead=lead)
        if choice == VIEW_PRO:
            self.show_pro()
        elif isinstance(choice, LocalAction):
            self.run_action(choice.action, dict(choice.inputs), label=choice.label)

    # ------------------------------------------------------------------ actions
    @property
    def actions(self) -> ActionExecutor:
        """The user's own deterministic actions (Free and Pro alike)."""
        if self._actions is None:
            self._actions = ActionExecutor.for_user(self.app, self.ui)
        return self._actions

    @property
    def events(self) -> Any:
        return self.app.ctx.events

    def resolver_context(self) -> ResolverContext:
        if self._resolver_context is None:
            self._resolver_context = ResolverContext.from_app(self.app)
        return self._resolver_context

    def run_resolution(self, resolution: Resolution, decision: Decision | None = None) -> bool:
        """Run a plan step by step — each through the action executor (risk, approval), then its
        verification; stop at the first step that does not succeed."""
        from highhx.decision.deterministic import LOCAL
        from highhx.decision.deterministic import Decision as LocalDecision
        from highhx.plans.planner import build_plan
        from highhx.plans.runner import PlanRunner, StepOutcome
        from highhx.plans.schema import PlanStep

        self.events.emit(
            ev.INTENT_RESOLVED,
            text=resolution.text,
            rule=resolution.rule,
            actions=[s.action for s in resolution.steps],
        )
        if decision is None or decision.plan is None:
            plan = build_plan(resolution.text, list(resolution.steps), self.actions.catalog, self._risk_of())
            decision = LocalDecision(
                resolution.text,
                resolution.text,
                LOCAL,
                intent=plan.intent,
                target=plan.target,
                plan=plan,
                resolution=resolution,
                rule=resolution.rule,
            )
        assert decision.plan is not None
        trace = self.trace(decision)

        def execute(step: PlanStep) -> ActionResult | None:
            return self.run_action(step.catalog_action, dict(step.params), label=step.description)

        def verified(done: StepOutcome) -> None:
            if done.check is not None and done.action_ok:
                self.ui.step_verified(done.step.description, done.check.status, done.check.detail)

        root = self.app.root if self.app is not None else None
        outcome = PlanRunner(execute, root=root, on_verified=verified, trace=trace).run(decision.plan)
        steps = list(resolution.steps)
        failed = outcome.failed_step
        if len(outcome.steps) > 1 or (failed is not None and failed.check is not None and failed.status == "failed"):
            done = sum(1 for s in outcome.steps if s.ok)
            self.ui.run_summary(
                trace.run_id,
                done,
                len(outcome.steps),
                outcome.verification,
                outcome.seconds,
                outcome.reason if len(outcome.steps) > 1 else "",
            )
        if failed is not None:
            index = next(i for i, s in enumerate(outcome.steps) if s is failed)
            self.retryable = ("steps", steps[index:])
            self.last_summary = f"{failed.step.description}: {failed.error or failed.status}."
            return False
        self.retryable = None
        return True

    def _risk_of(self) -> Any:
        from highhx.plans.planner import catalog_risk

        executor = self.actions
        fallback = catalog_risk(executor.catalog)

        def risk_of(action: str, params: dict[str, Any]) -> Any:
            try:
                return executor.plan(action, params).decision.risk
            except HighhXError:
                return fallback(action, params)

        return risk_of

    def run_action(
        self, name: str, inputs: dict[str, Any], *, label: str | None = None, planned: Planned | None = None
    ) -> ActionResult | None:
        """Plan (unless already planned) and execute one catalog action with live activity,
        approvals and Ctrl+C. A ``planned`` action was previewed and approved with /approve."""
        preapproved = planned is not None
        if planned is None:
            try:
                planned = self.actions.plan(name, inputs)
            except HighhXError as exc:
                self.ui.notice("error", exc.message)
                for detail in exc.details[:5]:
                    self.ui.print(f"  [dim]{escape(detail)}[/dim]")
                return None
        shown = label or name
        detail = f"{name} · {planned.decision.risk.label}"
        token = CancellationToken()
        self._token = token
        self.ui.activity_started(shown, detail=detail)
        result: ActionResult | None = None
        try:
            with self.local_io(), turn_interrupts(token):
                result = self.actions.execute(planned, cancel=token, preapproved=preapproved)
        except (KeyboardInterrupt, OperationCancelledError):
            result = ActionResult(False, status="cancelled", error="cancelled")
        except Exception as exc:  # a bug in one action must not end the session
            self.unexpected(exc)
            result = ActionResult(False, status="failed", error=f"{type(exc).__name__}: {exc}")
        self.ui.action_finished(shown, result)
        self.stats["actions"] += 1
        if not result.ok:
            self.stats["failed"] += 1
        self.last_summary = f"{shown}: done." if result.ok else f"{shown}: {result.error or result.status}."
        if name.split(".", 1)[0] in ("project", "workflow", "service", "deployment") or result.changed:
            self._resolver_context = None  # the project may have changed
        return result

    def shell(self, command: str) -> ActionResult | None:
        """``!command``: the shell.run action — classified, policy-checked, approved, audited."""
        if not command:
            self.ui.notice("info", "Usage: !<command>, e.g. !ls -la or !git log --oneline -5")
            return None
        return self.run_action("shell.run", {"command": command}, label=command)

    def command(self, argv: list[str], *, label: str | None = None) -> int:
        """Run a HighhX command in this session (the same code path as on the command line)."""
        if nested_session(argv):
            self.ui.notice("info", "You are already in the HighhX session — just type your request.")
            return 0
        shown = label or "highhx " + " ".join(argv)
        code = self._local(shown, lambda: self._invoke(argv))
        words = command_words(argv)
        if words and words[0] in ACCOUNT_COMMANDS:
            self.refresh_entitlements()
        return code

    def _invoke(self, argv: list[str]) -> int:
        from highhx.actions.invoke import invoke_cli

        return invoke_cli(self.app, argv)

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
        except Exception as exc:  # a bug in one command must not end the session
            self.unexpected(exc)
            code = int(ExitCode.FAILURE)
        self.ui.activity_finished(label, code, time.monotonic() - started)
        return code

    def unexpected(self, exc: BaseException) -> None:
        from highhx.ui.errors import render_unexpected

        log.exception("unexpected error in the interactive session")
        self.ui.assistant_finished()
        render_unexpected(self.ui.console, exc, self.ui.symbols, debug=self.app.options.debug)
        self.ui.print("  [dim]The session continues. Run with --debug for the traceback.[/dim]")

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
    def reconnect(self) -> bool:
        """Signed in but offline: re-check the platform (at most every RECONNECT_SECONDS).
        True when the check ran."""
        if (
            self.cloud is None
            or self.entitlements.connection not in (Connection.CACHED, Connection.UNAVAILABLE)
            or time.monotonic() - self._last_check < RECONNECT_SECONDS
        ):
            return False
        self.refresh_entitlements()
        return True

    def refresh_entitlements(self) -> None:
        """Re-check the account (after login / logout / upgrade, or a plan error) and adapt in place."""
        if self.cloud is None:
            return
        before = self.entitlements
        self._last_check = time.monotonic()
        self.entitlements = capabilities.resolve(self.cloud)
        pro = advanced_reasoning_available(self.entitlements)  # the Pro-only gate (JEv / the agent)
        if pro and self.session is None and self.session_factory is not None:
            try:
                self.session, self.account, _ = self.session_factory()
            except HighhXError as exc:
                self.ui.notice("warn", f"The AI agent could not start: {exc.message}")
                self.entitlements = dataclasses.replace(self.entitlements, capabilities=LOCAL, problem=exc.message)
                return
            self.ui.notice("info", f"{self.entitlements.status} — the AI agent now handles your requests.")
        elif not pro and self.session is not None:
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
        if self.session is not None:
            self.ui.print(
                "\n[bold]Describe what you want[/bold] — the AI agent plans it, you approve changes, HighhX "
                "verifies them. [dim]e.g. fix the login bug and make sure all tests pass[/dim]\n"
            )
        else:
            self.ui.print(
                "\n[bold]Describe what you want.[/bold] HighhX Free understands known actions without AI and "
                "chains them with “and” / “then”:\n"
                "  developer   [italic]run the tests[/italic] · [italic]check git changes[/italic] · "
                "[italic]deploy staging[/italic] · [italic]run python hello.py[/italic]\n"
                "  computer    [italic]open Safari[/italic] · [italic]open YouTube and play …[/italic] · "
                "[italic]open Chrome and search for …[/italic] · [italic]press cmd+t[/italic]\n"
                "  files       [italic]create a folder called …[/italic] · [italic]open hello.py[/italic] · "
                "[italic]list files in src[/italic] · [italic]open the project folder[/italic]\n"
                "Anything it doesn't know yet, it says so — open-ended tasks are for the AI agent (/pro).\n"
            )
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold magenta", no_wrap=True)
        table.add_column()
        table.add_column(style="dim", no_wrap=True)
        for group in HELP_GROUPS:
            table.add_row(f"[bold]{escape(group)}[/bold]", "", "")
            for command in (c for c in SLASH_COMMANDS if c.group == group):
                table.add_row(f"  {escape(command.usage)}", escape(command.help), "Pro" if command.agent else "")
        self.ui.print(table)
        self.ui.print(
            "\n[dim]!<command> shell command (risk-checked) · highhx <command> any HighhX command · "
            '\\ or """ multi-line · Ctrl+C interrupts · Ctrl+D exits[/dim]'
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
            rows.append(("Account", "[dim]not signed in — /login for HighhX Pro[/dim]"))
        # What is waiting on you — each line names the command that acts on it.
        if self.pending is not None:
            text, plans = self.pending
            rows.append(("Pending plan", f"{escape(text)} [dim]({len(plans)} step(s) — /approve or /deny)[/dim]"))
        if self.retryable is not None:
            kind, payload = self.retryable
            if kind == "turn":
                what = str(payload)
            elif kind == "task":
                what = f"task: {payload[0]}"
            else:
                what = ", ".join(step.action for step in payload)
            rows.append(("Last failure", f"{escape(str(what))} [dim](/retry)[/dim]"))
        from highhx.workflows import running

        active = running.running()
        if active:
            names = ", ".join(f"{r.workflow} {r.execution_id[-6:]}" for r in active)
            rows.append(("Running", f"{escape(names)} [dim](/cancel)[/dim]"))
        changed = len({p for j in self._journals() for p in j.changed_paths()})
        done = self.stats["actions"] - self.stats["failed"]
        activity = (
            f"{self.stats['actions']} action(s) · {done} ok · {self.stats['failed']} not ok · {changed} file(s) changed"
        )
        rows.append(("This session", f"{activity} [dim](/history, /changes)[/dim]"))
        if self.last_task is not None:
            task = self.last_task
            hint = "" if task.ok else " [dim](/retry)[/dim]"
            rows.append(("Last task", f"{escape(task.goal)} — {task.status} after {task.attempts} attempt(s){hint}"))
        s = self.session
        if s is not None:
            record = s.record
            rows += [
                ("Agent session", escape(record.id if record else "not saved") + f" [dim]· {s.turns} turn(s)[/dim]"),
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

    def cmd_tools(self, arg: str) -> None:
        catalog = self.actions.catalog
        if arg:
            specs = catalog.categories().get(arg.strip().lower())
            if not specs:
                self.ui.notice("warn", f"No action category '{arg}'. Categories: {', '.join(catalog.categories())}")
                return
            table = Table(box=None, header_style="dim", pad_edge=False)
            for column in ("action", "risk", "description"):
                table.add_column(column)
            for spec in specs:
                table.add_row(escape(spec.name), spec.risk.label, escape(spec.description))
            self.ui.print(table)
            return
        table = Table(box=None, header_style="dim", pad_edge=False)
        table.add_column("capability")
        table.add_column("")
        for capability in Capability:
            available = self.entitlements.has(capability)
            mark = "[ok]available[/ok]" if available else "[dim]HighhX Pro[/dim]"
            table.add_row(escape(LABELS[capability]), mark)
        self.ui.print(table)
        grouped = catalog.categories()
        self.ui.print(f"\n[bold]{len(catalog)} actions[/bold] [dim](run any with /run, or just ask)[/dim]")
        rows = Table.grid(padding=(0, 2))
        rows.add_column(style="bold magenta", no_wrap=True)
        rows.add_column(overflow="fold")
        for category, specs in grouped.items():
            rows.add_row(category, escape("  ".join(s.name.split(".", 1)[1] for s in specs)))
        self.ui.print(rows)
        if self.session is not None:
            names = self.session.registry.names()
            self.ui.print(f"\n[dim]Agent tools ({len(names)}):[/dim] {escape(', '.join(names))}")
        self.ui.print("\n[dim]/tools <category> lists a category with risk levels.[/dim]")

    def cmd_run(self, arg: str) -> None:
        name, _, rest = arg.strip().partition(" ")
        if not name:
            self.ui.notice("info", "Usage: /run <action> [key=value …]  e.g. /run filesystem.read path=README.md")
            return
        try:
            inputs = parse_inputs(rest)
        except ValueError as exc:
            self.ui.notice("warn", str(exc))
            return
        result = self.run_action(name, inputs)
        if result is not None and not result.ok:
            from highhx.actions.resolver import Step

            self.retryable = ("steps", [Step(name, inputs, name)])
        elif result is not None:
            self.retryable = None  # /retry is about the last request

    def cmd_approve(self, _arg: str) -> None:
        if self.pending is None:
            self.ui.notice("info", "No plan is waiting. /plan <request> previews one; /approve then runs it.")
            return
        text, plans = self.pending
        self.pending = None
        self.events.emit(ev.APPROVAL_GRANTED, text=text, actions=[p.spec.name for p in plans], mode="plan")
        for index, planned in enumerate(plans):
            result = self.run_action(planned.spec.name, planned.inputs, label=planned.spec.name, planned=planned)
            if result is None or not result.ok:
                from highhx.actions.resolver import Step

                self.retryable = ("steps", [Step(p.spec.name, p.inputs, p.spec.name) for p in plans[index:]])
                return

    def cmd_deny(self, _arg: str) -> None:
        if self.pending is None:
            self.ui.notice("info", "No plan is waiting. /plan <request> previews one.")
            return
        text, plans = self.pending
        self.pending = None
        self.events.emit(ev.APPROVAL_DENIED, text=text, actions=[p.spec.name for p in plans], mode="plan")
        self.ui.notice("info", "Plan discarded; nothing ran.")

    def cmd_retry(self, _arg: str) -> None:
        if self.retryable is None:
            self.ui.notice("info", "Nothing to retry — the last request succeeded.")
            return
        kind, payload = self.retryable
        self.retryable = None
        if kind == "task":
            goal, keys = payload
            if self.session is not None:
                self.run_task(goal, keys)
            return
        if kind == "turn":
            if self.session is None:
                self.local_request(str(payload))
            else:
                self.turn(str(payload))
            return
        from highhx.actions.resolver import Resolution

        self.run_resolution(Resolution(tuple(payload), "retry", "retry"))

    def cmd_resume(self, arg: str) -> None:
        run_id = arg.strip()
        if not run_id and self.app.history is not None:
            runs = [
                r
                for r in self.app.history.list(kind="workflow", limit=50)
                if r.status in ("failed", "cancelled", "timeout")
            ]
            run_id = runs[0].id if runs else ""
        if not run_id:
            self.ui.notice("info", "No failed or cancelled workflow run to resume. /workflow runs lists recent runs.")
            return
        self.command(["workflow", "resume", run_id])

    def cmd_cancel(self, arg: str) -> None:
        from highhx.workflows import running

        active = running.running()
        run_id = arg.strip() or (active[0].execution_id if len(active) == 1 else "")
        if not run_id:
            if not active:
                self.ui.notice("info", "No workflow is running. Ctrl+C interrupts what this session is running.")
            else:
                self.ui.notice("info", "Several workflows are running — /cancel <run-id>:")
                for entry in active:
                    self.ui.print(f"  [dim]{entry.execution_id}  {escape(entry.workflow)}  pid {entry.pid}[/dim]")
            return
        self.command(["workflow", "cancel", run_id])

    def cmd_task(self, arg: str) -> None:
        from highhx.agent.tasks import CHECKS, definition_of_done

        words = arg.split()
        keys: list[str] | None = None
        attempts: int | None = None
        goal_words: list[str] = []
        index = 0
        while index < len(words):
            word = words[index]
            if word == "--verify" and index + 1 < len(words):
                keys = [k.strip() for k in words[index + 1].split(",") if k.strip() and k.strip() != "none"]
                index += 2
                continue
            if word == "--attempts" and index + 1 < len(words) and words[index + 1].isdigit():
                attempts = max(1, min(10, int(words[index + 1])))
                index += 2
                continue
            goal_words.append(word)
            index += 1
        goal = " ".join(goal_words)
        if not goal:
            self.ui.notice("info", "Usage: /task <goal> [--verify test,check,build] [--attempts N]")
            return
        unknown = [k for k in keys or [] if k not in CHECKS]
        if unknown:
            self.ui.notice("warn", f"Unknown check(s): {', '.join(unknown)}. Use test, check or build.")
            return
        self.run_task(goal, keys if keys is not None else definition_of_done(goal), attempts=attempts)

    def cmd_doctor(self, _arg: str) -> None:
        self.run_action("security.doctor", {}, label="doctor")

    def cmd_init(self, _arg: str) -> None:
        if self.context.initialized:
            self.ui.notice("info", "HighhX is already set up here (.highhx/). /config shows the configuration.")
            return
        result = self.run_action("project.init", {}, label="set up HighhX")
        if result is not None and result.ok:
            self._context = None
            self._resolver_context = None
            self.ui.print("[dim]Next: try “run the tests”, or /workflows to see what was created.[/dim]")

    def cmd_login(self, _arg: str) -> None:
        self.command(["login"])

    def cmd_voice(self, arg: str) -> None:
        choice = arg.strip().lower() or ("off" if self._voice is not None and self._voice.active else "on")
        voice = self.voice
        if choice == "on":
            voice.enable()
        elif choice == "off":
            voice.disable()
        elif choice in ("mute", "unmute"):
            voice.set_muted(choice == "mute")
        elif choice == "status":
            rows = [("Voice", "on" if voice.active else "off"), ("Replies", "muted" if voice.muted else "spoken")]
            rows += [(k.capitalize(), v) for k, v in voice.engines.describe().items()]
            self._grid(rows)
            for note in voice.engines.notes:
                self.ui.print(f"  [dim]• {escape(note)}[/dim]")
        else:
            self.ui.notice("warn", "Use /voice on, off, mute, unmute or status.")

    def cmd_workflows(self, _arg: str) -> None:
        self.command(["workflow", "list"])

    def cmd_workflow(self, arg: str) -> None:
        try:
            argv = shlex.split(arg)
        except ValueError as exc:
            self.ui.notice("warn", f"Could not parse: {exc}")
            return
        if not argv:
            self.ui.notice("info", "Usage: /workflow <create|list|run|inspect|cancel|resume> …")
            return
        self.command(["workflow", *argv])

    def cmd_plan(self, arg: str) -> None:
        if arg.strip():
            self.preview(arg.strip())
            return
        plan = self.session.plan if self.session is not None else None
        if plan is None:
            self.ui.notice(
                "info",
                "Usage: /plan <request> previews what it would run."
                + (" Ask the agent for multi-step work and it proposes a plan first." if self.session else ""),
            )
            return
        counts = plan.counts()
        self.ui.print(f"\n[bold]Plan[/bold] [dim]— {escape(plan.goal)}[/dim]")
        self.ui.print(self.ui.plan_table(plan))
        self.ui.print(
            f"[dim]{counts['done']} done · {counts['failed']} failed · {counts['pending'] + counts['in_progress']} remaining[/dim]"
        )

    def preview(self, text: str) -> None:
        """What a request would run — resolved deterministically — without running anything."""
        planned = route(text, self.resolver_context())
        if planned.resolution is None:
            where = "the AI agent would plan and run it" if self.session else "it needs the AI agent (HighhX Pro)"
            self.ui.notice("info", f"Not a known deterministic request — {where}.")
            for alternative in planned.alternatives:
                self.ui.print(f"  [dim]→ {escape(alternative.action)}  {escape(alternative.label)}[/dim]")
            return
        table = Table(box=None, header_style="dim", pad_edge=False)
        for column in ("#", "action", "inputs", "risk", "approval"):
            table.add_column(column)
        for number, step in enumerate(planned.resolution.steps, start=1):
            try:
                p = self.actions.plan(step.action, dict(step.inputs))
            except HighhXError as exc:
                table.add_row(str(number), escape(step.action), "", "", f"[fail]{escape(exc.message)}[/fail]")
                continue
            approval = "blocked" if p.decision.blocked else ("asks" if p.asks else "runs")
            inputs = ", ".join(f"{k}={v}" for k, v in step.inputs.items())
            table.add_row(str(number), escape(step.action), escape(inputs), p.decision.risk.label, approval)
        self.ui.print(f"[dim]{escape(planned.resolution.description)}[/dim]")
        self.ui.print(table)
        plans: list[Planned] = []
        for step in planned.resolution.steps:
            try:
                plans.append(self.actions.plan(step.action, dict(step.inputs)))
            except HighhXError:
                self.pending = None
                return
        if any(p.decision.blocked for p in plans):
            self.pending = None
            self.ui.print("[warn]A step is blocked by policy — this plan cannot run.[/warn]")
            return
        self.pending = (planned.resolution.text, plans)
        typed = any(p.decision.approval >= Approval.TYPED for p in plans)
        self.ui.print(
            "[dim]/approve runs exactly this plan"
            + (" (critical steps still ask you to type approve)" if typed else "")
            + " · /deny discards it[/dim]"
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
        from highhx.actions.events import read_events

        marks = {
            ev.INTENT_RESOLVED: "[magenta]❯[/magenta]",  # noqa: RUF001
            ev.ACTION_COMPLETED: f"[ok]{self.ui.symbols.ok}[/ok]",
            ev.ACTION_FAILED: f"[fail]{self.ui.symbols.fail}[/fail]",
            ev.APPROVAL_DENIED: f"[warn]{self.ui.blocked_mark}[/warn]",
        }
        records = [r for r in read_events(session=self.session_id, limit=80) if r.get("event") in marks]
        if records:
            table = Table.grid(padding=(0, 2))
            table.add_column(style="dim", no_wrap=True)
            table.add_column(no_wrap=True)
            table.add_column(overflow="fold")
            for r in records[-25:]:
                name = str(r.get("event"))
                if name == ev.INTENT_RESOLVED:
                    what = f"[bold]{escape(str(r.get('text', '')))}[/bold]"
                elif name == ev.APPROVAL_DENIED:
                    what = (
                        f"{escape(str(r.get('action') or ', '.join(r.get('actions') or [])))} [dim]— not approved[/dim]"
                    )
                elif name == ev.ACTION_FAILED:
                    what = f"{escape(str(r.get('action')))} [dim]— {escape(str(r.get('error') or r.get('status'))[:80])}[/dim]"
                else:
                    what = f"{escape(str(r.get('action')))} [dim]— {escape(str(r.get('summary', ''))[:80])}[/dim]"
                table.add_row(str(r.get("ts", ""))[11:19], marks[name], what)
            self.ui.print(table)
        else:
            self.ui.notice("info", "Nothing has run in this session yet — try “run the tests”.")
        if self.session is not None and self.session.store is not None:
            sessions = self.session.store.list(root=str(self.session.app.root), limit=10)
            if sessions:
                table = Table(box=None, header_style="dim", pad_edge=False)
                for column in ("session", "updated", "turns", "tokens", "title"):
                    table.add_column(column)
                current = self.session.record.id if self.session.record else None
                for rec in sessions:
                    marker = " [magenta]●[/magenta]" if rec.id == current else ""
                    table.add_row(
                        escape(rec.id) + marker,
                        rec.updated_at.replace("T", " ")[:16],
                        str(rec.turns),
                        format_tokens(rec.usage.total),
                        escape(rec.title),
                    )
                self.ui.print()
                self.ui.print(table)
                self.ui.print("[dim]Resume one with `highhx agent --resume <id>`.[/dim]")
        self.ui.print("[dim]Everything HighhX ran in this project: `highhx history` · events: `highhx events`.[/dim]")

    def _journals(self) -> list[Any]:
        journals = [self.actions.journal]
        if self.session is not None:
            journals.append(self.session.journal)
        return journals

    def cmd_changes(self, _arg: str) -> None:
        first: dict[Any, Any] = {}
        for journal in self._journals():
            for turn in journal.turns:
                for change in turn:
                    first.setdefault(change.path, change)
        if not first:
            self.ui.notice("info", "No files changed in this session. Changes by actions and the agent appear here.")
            return
        root = self.app.root.resolve()
        for path, change in first.items():
            state = "deleted" if not path.exists() else ("created" if change.before is None else "modified")
            style = {"created": "ok", "deleted": "fail"}.get(state, "warn")
            shown = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
            self.ui.print(f"  [{style}]{state:<8}[/{style}] {escape(shown)}")
        self.ui.print(
            f"[dim]{len(first)} file(s) · /undo reverts the most recent change · git diff shows details[/dim]"
        )

    def cmd_undo(self, _arg: str) -> None:
        if self.session is not None and self.session.journal.last_changes():
            restored = self.session.undo()
            if restored:
                self.ui.notice("info", f"Restored {len(restored)} file(s): {', '.join(restored)}")
                self.session.note(
                    f"The user undid your last changes to: {', '.join(restored)}. Those edits are reverted."
                )
                return
        paths = self.actions.journal.undo_last()
        if not paths:
            self.ui.notice("info", "Nothing to undo — no files changed in this session.")
            return
        root = self.app.root.resolve()
        shown = [p.relative_to(root).as_posix() if p.is_relative_to(root) else str(p) for p in paths]
        self.ui.notice("info", f"Restored {len(shown)} file(s): {', '.join(shown)}")

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
