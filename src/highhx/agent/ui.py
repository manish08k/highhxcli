"""The HighhX interactive terminal experience (Rich), shared by Free and Pro.

Streams the agent's answer as Markdown, shows a live activity line while tools
and local commands run (with the latest line of command output), prints outcomes
(✓ done, ✗ failed, ⊘ blocked or declined, ○ cancelled), renders plans and diffs,
asks for approvals without fighting the live display, and explains capabilities
that need HighhX Pro.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from rich.box import ROUNDED
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.status import Status
from rich.syntax import Syntax
from rich.table import Table

from highhx.agent.messages import ToolCall
from highhx.agent.planner import Plan
from highhx.agent.tools.base import Tool, ToolResult
from highhx.safety.confirmation import ConfirmationRequest
from highhx.ui.terminal import Symbols

if TYPE_CHECKING:
    from highhx.agent.context import ProjectContext
    from highhx.agent.router import LocalAction

QUIET_TOOLS = frozenset({"update_plan", "propose_plan"})
"""Tools whose progress is shown by plan rendering instead of an activity line."""
MAX_DIFF_LINES = 80
ACCENT = "bold magenta"
VIEW_PRO = "pro"
"""Answer from :meth:`TerminalUI.capability_panel` when the user wants to see HighhX Pro."""
BLOCKED_CODES = frozenset({"policy", "denied"})
BORDERS = {"warn": "yellow", "fail": "red"}
"""Concrete border colours for theme styles (a border style must resolve on any console)."""


def format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


class TerminalUI:
    def __init__(
        self,
        console: Console,
        symbols: Symbols,
        *,
        interactive: bool,
        read_line: Callable[[str], str] | None = None,
    ) -> None:
        self.console = console
        self.symbols = symbols
        self._interactive = interactive
        self._read_line = read_line or console.input
        self._lock = threading.RLock()
        self._status: Status | None = None
        self._status_label = ""
        self._live: Live | None = None
        self._text: list[str] = []
        self._streamed_plain = False

    @property
    def interactive(self) -> bool:
        return self._interactive

    @property
    def fancy(self) -> bool:
        return self.console.is_terminal

    # --------------------------------------------------------------- spinner
    def _spin(self, label: str) -> None:
        with self._lock:
            self._status_label = label
            if not self.fancy:
                return
            if self._status is None:
                self._status = self.console.status(label, spinner="dots", spinner_style="magenta")
                self._status.start()
            else:
                self._status.update(label)

    def _stop_spin(self) -> None:
        with self._lock:
            if self._status is not None:
                self._status.stop()
                self._status = None

    def _pause(self) -> None:
        """Stop live output before printing a question."""
        self._stop_spin()
        self._stop_live()

    # ------------------------------------------------------------ assistant
    def assistant_started(self) -> None:
        self._text = []
        self._streamed_plain = False
        self._spin("Thinking…")

    def assistant_text(self, delta: str) -> None:
        with self._lock:
            if not self._text:
                self._stop_spin()
            self._text.append(delta)
            if self.fancy:
                if self._live is None:
                    self._live = Live(
                        Markdown(""), console=self.console, refresh_per_second=12, vertical_overflow="visible"
                    )
                    self._live.start()
                self._live.update(Markdown("".join(self._text)))
            else:
                self.console.file.write(delta)
                self.console.file.flush()
                self._streamed_plain = True

    def _stop_live(self) -> None:
        with self._lock:
            if self._live is not None:
                self._live.stop()
                self._live = None

    def assistant_finished(self) -> None:
        self._stop_spin()
        self._stop_live()
        if self._streamed_plain:
            self.console.file.write("\n")
            self.console.file.flush()
            self._streamed_plain = False
        self._text = []

    # ----------------------------------------------------------------- tools
    def tool_started(self, tool: Tool | None, call: ToolCall, description: str) -> None:
        if tool is not None and tool.name in QUIET_TOOLS:
            return
        self._spin(f"[bold]{escape(description)}[/bold]")

    def tool_output(self, line: str) -> None:
        if not self.fancy:
            return
        text = line.strip()
        if not text:
            return
        with self._lock:
            if self._status is not None:
                shown = escape(text[:100])
                self._status.update(f"{self._status_label}\n  [dim]{shown}[/dim]")

    def tool_finished(self, tool: Tool | None, call: ToolCall, result: ToolResult, seconds: float) -> None:
        self._stop_spin()
        if tool is not None and tool.name in QUIET_TOOLS and result.ok:
            return
        description = tool.describe(call.input) if tool is not None else call.name
        timing = f" [dim]({seconds:.1f}s)[/dim]" if seconds >= 1 else ""
        if result.ok:
            summary = f" [dim]— {escape(result.summary)}[/dim]" if result.summary else ""
            self.console.print(f"[ok]{self.symbols.ok}[/ok] {escape(description)}{summary}{timing}")
            return
        first = result.content.strip().splitlines()[0][:120] if result.content.strip() else "failed"
        summary = escape(result.summary or first)
        if result.error_code in BLOCKED_CODES:
            self.console.print(f"[warn]{self.blocked_mark}[/warn] {escape(description)} [warn]— {summary}[/warn]")
        elif result.error_code == "cancelled":
            self.console.print(f"[dim]{self.symbols.skip} {escape(description)} — cancelled[/dim]")
        else:
            self.console.print(
                f"[fail]{self.symbols.fail}[/fail] {escape(description)} [fail]— {summary}[/fail]{timing}"
            )

    @property
    def blocked_mark(self) -> str:
        return "⊘" if self.symbols.ok == "✓" else "-"

    # -------------------------------------------------------- local activity
    def activity_started(self, label: str, *, detail: str = "") -> None:
        """A local (non-AI) action starts: a heading line that stays above the command's own output."""
        self._pause()
        suffix = f"  [dim]{escape(detail)}[/dim]" if detail else ""
        self.console.print()
        self.console.print(f"[{ACCENT}]◉[/{ACCENT}] [bold]{escape(label)}[/bold]{suffix}")

    def activity_finished(self, label: str, code: int, seconds: float) -> None:
        self._pause()
        timing = f" [dim]({seconds:.1f}s)[/dim]" if seconds >= 0.1 else ""
        if code == 0:
            self.console.print(f"[ok]{self.symbols.ok}[/ok] {escape(label)}{timing}")
        elif code == 130:
            self.console.print(f"[dim]{self.symbols.skip} {escape(label)} — cancelled[/dim]")
        else:
            self.console.print(
                f"[fail]{self.symbols.fail}[/fail] {escape(label)} [fail]— exit code {code}[/fail]{timing}"
            )

    # ------------------------------------------------------------ capability
    def capability_panel(
        self, reason: str, alternatives: Sequence[LocalAction], *, title: str = "HighhX Pro capability"
    ) -> LocalAction | str | None:
        """Explain a capability this session does not have and offer the local alternatives.

        Returns the chosen :class:`LocalAction`, :data:`VIEW_PRO`, or ``None`` (continue without it).
        """
        self._pause()
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True, style=ACCENT)
        table.add_column(overflow="fold")
        table.add_column(overflow="fold", style="dim")
        body: list[Any] = [f"[bold]{escape(reason)}[/bold]", ""]
        if alternatives:
            body.append("Available locally, without AI:")
            for number, action in enumerate(alternatives, start=1):
                table.add_row(str(number), escape(action.label), escape(action.command))
            body.append(table)
            body.append("")
        else:
            body += ["Nothing local maps to this request. Try /help for what runs locally.", ""]
        numbers = "1" if len(alternatives) == 1 else f"1-{len(alternatives)}"
        choices = ([f"\\[{numbers}] Continue locally"] if alternatives else []) + ["\\[p] View Pro"]
        body.append("[dim]" + "    ".join(choices) + "[/dim]")
        from rich.console import Group

        self.console.print()
        self.console.print(
            Panel(
                Group(*body),
                title=f"[{ACCENT}]{escape(title)}[/{ACCENT}]",
                title_align="left",
                border_style="magenta",
                box=ROUNDED,
                padding=(1, 2),
            )
        )
        if not self.interactive:
            return None
        numbers = "1" if len(alternatives) == 1 else f"1-{len(alternatives)}"
        hint = f"{numbers} / p / Enter to skip" if alternatives else "p / Enter to skip"
        answer = self._ask(f"[bold]Choose[/bold] [dim]\\[{hint}][/dim] ").strip().lower()
        if answer in ("p", "pro", "view pro"):
            return VIEW_PRO
        if answer.isdigit() and 1 <= int(answer) <= len(alternatives):
            return alternatives[int(answer) - 1]
        return None

    # ------------------------------------------------------------------ plan
    def plan_table(self, plan: Plan) -> Table:
        table = Table.grid(padding=(0, 1))
        table.add_column(justify="right", style="dim")
        table.add_column()
        marks = {
            "pending": f"[dim]{self.symbols.skip}[/dim]",
            "in_progress": f"[info]{self.symbols.arrow}[/info]",
            "done": f"[ok]{self.symbols.ok}[/ok]",
            "failed": f"[fail]{self.symbols.fail}[/fail]",
            "skipped": f"[dim]{self.symbols.skip}[/dim]",
        }
        for number, step in enumerate(plan.steps, start=1):
            note = f" [dim]— {escape(step.note)}[/dim]" if step.note else ""
            mark = marks.get(step.status, "") if plan.approved else ""
            table.add_row(f"{number}.", f"{mark} {escape(step.title)}{note}".strip())
        return table

    def present_plan(self, plan: Plan) -> tuple[bool, str]:
        self._pause()
        self.console.print()
        self.console.print(
            Panel(
                self.plan_table(plan),
                title=f"[{ACCENT}]Plan[/{ACCENT}]",
                title_align="left",
                subtitle=f"[dim]{escape(plan.goal)}[/dim]",
                subtitle_align="left",
                border_style="magenta",
                box=ROUNDED,
                padding=(1, 2),
            )
        )
        if not self.interactive:
            self.console.print("[dim]Proceeding (non-interactive session).[/dim]")
            return True, ""
        answer = self._ask("[bold]Proceed?[/bold] [dim]\\[Y/n][/dim] ").strip().lower()
        if answer in ("", "y", "yes"):
            self.console.print()
            return True, ""
        feedback = self._ask("[bold]What should I change?[/bold] [dim](Enter to stop)[/dim] ").strip()
        return False, feedback

    def plan_updated(self, plan: Plan, index: int) -> None:
        step = plan.steps[index]
        note = f" [dim]— {escape(step.note)}[/dim]" if step.note else ""
        if step.status == "in_progress":
            self._spin(f"[bold]{escape(step.title)}[/bold]")
            return
        self._stop_spin()
        symbol = {
            "done": f"[ok]{self.symbols.ok}[/ok]",
            "failed": f"[fail]{self.symbols.fail}[/fail]",
            "skipped": f"[dim]{self.symbols.skip}[/dim]",
        }.get(step.status, self.symbols.bullet)
        self.console.print(f"{symbol} [bold]{escape(step.title)}[/bold]{note}")

    # ------------------------------------------------------------- approvals
    def _ask(self, prompt: str) -> str:
        try:
            return self._read_line(prompt)
        except EOFError:
            return ""

    def _render_details(self, details: Sequence[str]) -> None:
        for detail in details:
            if detail.startswith(("--- ", "+++ ", "@@")) or "\n@@ " in detail:
                lines = detail.splitlines()
                shown = "\n".join(lines[:MAX_DIFF_LINES])
                self.console.print(
                    Panel(
                        Syntax(shown, "diff", theme="ansi_dark", word_wrap=True, background_color="default"),
                        border_style="dim",
                        box=ROUNDED,
                        padding=(0, 1),
                    )
                )
                if len(lines) > MAX_DIFF_LINES:
                    self.console.print(f"  [dim]… {len(lines) - MAX_DIFF_LINES} more diff lines[/dim]")
            else:
                self.console.print(f"  [dim]{self.symbols.bullet} {escape(detail)}[/dim]")

    def ask_permission(self, action: str, details: Sequence[str], *, allow_always: bool = True) -> str:
        self._pause()
        self.console.print()
        self._approval_panel("Action requires approval", f"[bold]{escape(action)}[/bold]", "warn")
        self._render_details(details)
        choices = "\\[y/N/a=always this session]" if allow_always else "\\[y/N]"
        answer = self._ask(f"[bold]Proceed?[/bold] [dim]{choices}[/dim] ").strip().lower()
        if answer in ("y", "yes"):
            return "yes"
        if allow_always and answer in ("a", "always"):
            return "always"
        return "no"

    def ask(self, question: str, *, default: bool = False) -> bool:
        """A plain yes/no question (a choice, not a risk approval)."""
        self._pause()
        if not self.interactive:
            return default
        suffix = "\\[Y/n]" if default else "\\[y/N]"
        answer = self._ask(f"[bold]{escape(question)}[/bold] [dim]{suffix}[/dim] ").strip().lower()
        return default if not answer else answer in ("y", "yes")

    def _approval_panel(self, title: str, body: str, style: str) -> None:
        self.console.print(
            Panel(
                body,
                title=f"[{style}]{self.symbols.warn} {title}[/{style}]",
                title_align="left",
                border_style=BORDERS.get(style, style),
                box=ROUNDED,
                padding=(0, 2),
            )
        )

    def confirm_action(self, request: ConfirmationRequest) -> bool:
        """The sensitive-action confirmation. Only an explicit approval returns True."""
        self._pause()
        self.console.print()
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", no_wrap=True)
        table.add_column(overflow="fold")
        risk_style = "fail" if request.risk.label == "critical" else "warn"
        table.add_row("Action", f"[bold]{escape(request.action)}[/bold]")
        table.add_row("Target", escape(request.target))
        table.add_row("Resource", escape(request.application))
        table.add_row("Tool", escape(request.tool))
        if request.command:
            table.add_row("Command", escape(request.command))
        irreversible = " — destructive / potentially irreversible" if request.irreversible else ""
        table.add_row("Risk", f"[{risk_style}]{request.risk.label}{irreversible}[/{risk_style}]")
        table.add_row("Why", escape("; ".join(request.reasons)))
        self.console.print(
            Panel(
                table,
                title=f"[{risk_style}]{self.symbols.warn} Approval required[/{risk_style}]",
                title_align="left",
                border_style=BORDERS.get(risk_style, risk_style),
                box=ROUNDED,
            )
        )
        self._render_details(request.details)
        if request.confirm_word:
            answer = self._ask(
                f"  Type [bold]{escape(request.confirm_word)}[/bold] to approve, anything else cancels: "
            ).strip()
            approved = answer == request.confirm_word
        else:
            answer = self._ask("[bold]Approve?[/bold] [dim]\\[y/N][/dim] ").strip().lower()
            approved = answer in ("y", "yes")
        self.console.print("[ok]Approved.[/ok]" if approved else "[warn]Cancelled.[/warn]")
        return approved

    def confirm(self, message: str, *, default: bool = False) -> bool:
        self._pause()
        self.console.print()
        self._approval_panel("Action requires approval", f"[bold]{escape(message)}[/bold]", "warn")
        suffix = "\\[Y/n]" if default else "\\[y/N]"
        answer = self._ask(f"[bold]Approve?[/bold] [dim]{suffix}[/dim] ").strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")

    def confirm_typed(self, message: str, expected: str) -> bool:
        self._pause()
        self.console.print()
        self._approval_panel("High-risk action requires approval", f"[bold]{escape(message)}[/bold]", "fail")
        answer = self._ask(f"  Type [bold]{escape(expected)}[/bold] to confirm: ").strip()
        return answer == expected

    # ---------------------------------------------------------------- notices
    def notice(self, level: str, message: str) -> None:
        with self._lock:
            style = {"warn": "warn", "error": "fail"}.get(level, "info")
            symbol = {"warn": self.symbols.warn, "error": self.symbols.fail}.get(level, self.symbols.info)
            printer = self._status.console if self._status is not None else self.console
            printer.print(f"[{style}]{symbol} {escape(message)}[/{style}]")

    # ------------------------------------------------------------ chrome
    def banner(
        self, context: ProjectContext, rows: list[tuple[str, str]], *, status: Sequence[tuple[str, str]] = ()
    ) -> None:
        """The Knight with version, location and ``status`` lines (git state, plan), then ``rows``."""
        from highhx import __version__
        from highhx.ui.branding import banner, home_relative

        self.console.print(
            banner(
                self.console,
                f"HighhX v{__version__}",
                "Developer command center",
                home_relative(str(context.root)),
                accent="bold magenta",
                status=status,
            )
        )
        self.console.print()
        if not rows:
            return
        table = Table.grid(padding=(0, 3))
        table.add_column(style="dim", no_wrap=True)
        table.add_column(overflow="fold")
        for key, value in rows:
            table.add_row(key, value)
        self.console.print(table)
        self.console.print()

    def footer(self, parts: list[str]) -> None:
        if parts:
            self.console.print(f"[dim]{'  ·  '.join(escape(p) for p in parts)}[/dim]")

    def markdown(self, text: str) -> None:
        self.console.print(Markdown(text))

    def print(self, renderable: Any = "") -> None:
        self.console.print(renderable)
