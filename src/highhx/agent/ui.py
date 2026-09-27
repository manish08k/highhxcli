"""The HighhX Pro terminal experience (Rich).

Streams the agent's answer as Markdown, shows a live activity line while tools
run (with the latest line of command output), prints ✓ / ✗ outcomes, renders
plans and diffs, and asks for approvals without fighting the live display.
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
from rich.text import Text

from highhx.agent.messages import ToolCall
from highhx.agent.planner import Plan
from highhx.agent.tools.base import Tool, ToolResult
from highhx.safety.confirmation import ConfirmationRequest
from highhx.ui.terminal import Symbols

if TYPE_CHECKING:
    from highhx.agent.context import ProjectContext

QUIET_TOOLS = frozenset({"update_plan", "propose_plan"})
"""Tools whose progress is shown by plan rendering instead of an activity line."""
MAX_DIFF_LINES = 80
ACCENT = "bold magenta"


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
        else:
            first = result.content.strip().splitlines()[0][:120] if result.content.strip() else "failed"
            summary = escape(result.summary or first)
            self.console.print(
                f"[fail]{self.symbols.fail}[/fail] {escape(description)} [fail]— {summary}[/fail]{timing}"
            )

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
        self.console.print(f"[warn]{self.symbols.warn} Action requires approval[/warn]")
        self.console.print(f"  [bold]{escape(action)}[/bold]")
        self._render_details(details)
        choices = "\\[y/N/a=always this session]" if allow_always else "\\[y/N]"
        answer = self._ask(f"[bold]Proceed?[/bold] [dim]{choices}[/dim] ").strip().lower()
        if answer in ("y", "yes"):
            return "yes"
        if allow_always and answer in ("a", "always"):
            return "always"
        return "no"

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
                title=f"[{risk_style}]Confirm Action[/{risk_style}]",
                title_align="left",
                border_style=risk_style,
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
        self.console.print(f"[warn]{self.symbols.warn} Action requires approval[/warn]")
        suffix = "\\[Y/n]" if default else "\\[y/N]"
        answer = self._ask(f"  {escape(message)} [dim]{suffix}[/dim] ").strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")

    def confirm_typed(self, message: str, expected: str) -> bool:
        self._pause()
        self.console.print()
        self.console.print(f"[fail]{self.symbols.warn} High-risk action requires approval[/fail]")
        self.console.print(f"  [bold]{escape(message)}[/bold]")
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
    def banner(self, context: ProjectContext, rows: list[tuple[str, str]]) -> None:
        title = Text.assemble(("HIGHHX PRO", "bold magenta"), "\n", ("AI DEVELOPER AGENT", "dim"))
        title.justify = "center"
        self.console.print(Panel(title, box=ROUNDED, border_style="magenta", padding=(0, 2)))
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
