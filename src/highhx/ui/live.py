"""The live HighhX dashboard: a view of the event stream, nothing more.

    ┌ HighhX ─────────── project · main · Pro · computer: local ─┐
    │ TASK   export the invoices            running · 12.4s       │
    │ PLAN   ✓ open the invoices   ● export the invoices  ○ …      │
    │ ACTION browser.click 'button:"Export"'  risk low · approved │
    │ STATE  browser · https://shop.test/invoices · 3 elements     │
    │ GROUND accessibility ✗  dom ✓ 0.95                           │
    │ VERIFY success          RECOVERY 0 / 10                      │
    │ TOOLS  ✓ browser.open 0.3s  ✓ browser.click 0.3s             │
    │ NETWORK POST https://shop.test/api/export 201                 │
    │ COST   1,240 tokens · 4 actions · avg 0.31s                  │
    │ ⚠ warnings / ✗ errors                         trace tr_9c1… │
    └──────────────────────────────────────────────────────────────┘

:class:`DashboardState` folds :class:`~highhx.observability.stream.EventRecord` s into what is
shown, and :func:`render` draws it. Neither executes anything: the TUI only watches the same
events the executor, agent loop, MCP server and benchmarks emit. Payloads arrive redacted.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import Console, RenderableType
    from rich.live import Live

    from highhx.observability.stream import EventRecord, EventRecorder

STATUS_MARK = {"done": "✓", "running": "●", "failed": "✗", "pending": "○", "skipped": "-"}


@dataclass
class DashboardState:
    project: str = ""
    branch: str = ""
    plan_name: str = "Free"
    account: str = ""
    connection: str = "local"
    runtime: str = "local"
    task: str = ""
    task_id: str = ""
    trace_id: str = ""
    status: str = "idle"
    agent_state: str = "idle"
    started: float | None = None
    ended: float | None = None
    plan: list[dict[str, Any]] = field(default_factory=list)
    action: str = ""
    action_detail: str = ""
    risk: str = ""
    approval: str = ""
    computer: str = ""
    grounding: list[tuple[str, str, float]] = field(default_factory=list)
    grounding_target: str = ""
    verification: str = ""
    recoveries: int = 0
    max_recoveries: int = 10
    recovery_reason: str = ""
    tools: deque[tuple[bool | None, str, float]] = field(default_factory=lambda: deque(maxlen=8))
    tokens: int = 0
    cost: float | None = None
    actions: int = 0
    action_seconds: float = 0.0
    warnings: deque[str] = field(default_factory=lambda: deque(maxlen=4))
    errors: deque[str] = field(default_factory=lambda: deque(maxlen=4))
    healed: int = 0
    summary: str = ""
    network: deque[str] = field(default_factory=lambda: deque(maxlen=3))
    """The latest requests browser actions caused (sanitized: no query values)."""
    network_failed: int = 0
    intervention: str = ""
    """Why the task is waiting for the person (a CAPTCHA, a secret field …)."""

    # --------------------------------------------------------------- events
    def apply(self, record: EventRecord) -> None:
        name, p = record.name, record.payload
        if record.trace_id and name in ("task.started", "agent.started"):
            self.trace_id = record.trace_id
        if name == "task.started":
            self.task, self.task_id = str(p.get("task", "")), record.task_id
            self.status, self.agent_state, self.started, self.ended = "running", "observing", time.monotonic(), None
            self.plan, self.grounding, self.verification, self.summary = [], [], "", ""
            self.recoveries, self.healed = 0, 0
            self.runtime = str(p.get("surface") or self.runtime)
        elif name == "agent.planning":
            self.agent_state = "planning"
        elif name == "plan.created":
            self.plan = [{"title": str(s), "status": "pending"} for s in p.get("steps") or []]
        elif name == "plan.updated":
            items = p.get("items")
            if isinstance(items, list):
                self.plan = [dict(i) for i in items if isinstance(i, dict)]
        elif name == "observation.created":
            where = p.get("url") or p.get("app") or ""
            self.computer = f"{p.get('surface', '')} · {where} · {p.get('elements', 0)} element(s)".strip(" ·")
            if self.status == "running" and self.agent_state not in ("verifying", "recovering"):
                self.agent_state = "observing"
        elif name == "grounding.started":
            self.grounding, self.grounding_target = [], str(p.get("target", ""))
            self.agent_state = "grounding"
        elif name == "grounding.attempt":
            self.grounding.append((str(p.get("strategy", "")), str(p.get("result", "")), float(p.get("best") or 0.0)))
        elif name == "action.planned":
            if p.get("action") in ("computer.state", "android.observe"):
                return
            self.action = str(p.get("action", ""))
            self.risk = str(p.get("risk", ""))
            self.approval = "asking" if p.get("asks") else "not needed"
            self.action_detail = ", ".join(f"{k}={v}" for k, v in (p.get("inputs") or {}).items() if k not in ("text", "content"))[:80]
            self.agent_state = "acting"
        elif name == "approval.requested":
            self.approval, self.agent_state = "waiting for you", "waiting for approval"
        elif name == "approval.granted":
            self.approval = "approved"
        elif name == "approval.denied":
            self.approval = "declined"
            self.warnings.append(f"declined: {p.get('action', '')}")
        elif name in ("action.completed", "action.failed"):
            if p.get("action") in ("computer.state", "android.observe"):
                return
            ok = name == "action.completed"
            seconds = float(p.get("seconds") or 0.0)
            self.tools.append((ok, str(p.get("action", "")), seconds))
            self.actions += 1
            self.action_seconds += seconds
            if not ok:
                self.errors.append(f"{p.get('action', '')}: {p.get('status', '')} {p.get('error', '')}".strip())
        elif name == "verification.started":
            self.agent_state = "verifying"
        elif name == "verification.completed":
            self.verification = str(p.get("outcome") or p.get("verdict") or "")
        elif name == "verification.failed":
            self.warnings.append(f"not verified: {p.get('detail', '')}")
        elif name == "recovery.started":
            self.recoveries += 1
            self.recovery_reason = f"{p.get('kind', '')}: {p.get('reason', '')}"
            self.agent_state = "recovering"
        elif name == "network.observed":
            for entry in (p.get("entries") or [])[-3:]:
                status = entry.get("status") or entry.get("error") or "…"
                self.network.append(f"{entry.get('method', '')} {entry.get('url', '')} {status}")
            self.network_failed += int(p.get("failed") or 0)
        elif name == "agent.reflection" and p.get("decision") == "ask_user":
            self.intervention = str(p.get("reason", ""))
            self.agent_state = "waiting for you"
        elif name == "agent.reflection" and p.get("decision") not in (None, "continue"):
            self.recovery_reason = f"{p.get('decision')}: {p.get('reason', '')}"
            if p.get("decision") in ("retry", "replan"):
                self.recoveries += 1
        elif name == "selector.healed":
            self.healed += 1
            self.warnings.append(f"healed {p.get('was')!r} → {p.get('now')!r} ({p.get('strategy')})")
        elif name == "model.usage":
            self.tokens += int(p.get("input_tokens") or 0) + int(p.get("output_tokens") or 0)
            if p.get("cost") is not None:
                self.cost = (self.cost or 0.0) + float(p["cost"])
        elif name == "sandbox.created":
            self.runtime = f"sandbox {p.get('sandbox', '')} ({p.get('isolation', '')})"
        elif name in ("task.completed", "task.failed"):
            self.status = str(p.get("status") or ("completed" if name == "task.completed" else "failed"))
            self.summary = str(p.get("summary", ""))
            self.agent_state, self.ended = "idle", time.monotonic()
            if name == "task.failed":
                self.errors.append(self.summary)

    @property
    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        return (self.ended or time.monotonic()) - self.started

    @property
    def progress(self) -> tuple[int, int]:
        done = sum(1 for i in self.plan if i.get("status") == "done")
        return done, len(self.plan)


def _status_style(status: str) -> str:
    return {"completed": "green", "running": "cyan", "failed": "red", "needs_user": "yellow", "interrupted": "yellow"}.get(status, "white")


def render(state: DashboardState) -> RenderableType:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="bold dim", width=8)
    grid.add_column(ratio=1)
    done, total = state.progress
    task = Text(state.task or "no task yet", style="bold")
    task.append(f"   {state.status}", style=_status_style(state.status))
    if state.started is not None:
        task.append(f" · {state.elapsed:.1f}s · {state.agent_state}", style="dim")
    grid.add_row("TASK", task)
    if state.plan:
        plan = Text()
        for item in state.plan[-8:]:
            status = str(item.get("status", "pending"))
            plan.append(f"{STATUS_MARK.get(status, '○')} ", style={"done": "green", "failed": "red", "running": "cyan"}.get(status, "dim"))
            plan.append(f"{item.get('title', '')}   ")
        plan.append(f"[{done}/{total}]", style="dim")
        grid.add_row("PLAN", plan)
    if state.action:
        action = Text(state.action, style="bold")
        if state.action_detail:
            action.append(f"  {state.action_detail}", style="dim")
        action.append(f"   risk {state.risk or '-'} · {state.approval or '-'}", style="yellow" if state.approval == "waiting for you" else "dim")
        grid.add_row("ACTION", action)
    if state.computer:
        grid.add_row("STATE", Text(state.computer))
    if state.grounding:
        ground = Text(f"{state.grounding_target!r}  " if state.grounding_target else "")
        for strategy, result, best in state.grounding:
            mark, style = {"success": ("✓", "green"), "failed": ("✗", "red"), "ambiguous": ("≈", "yellow")}.get(result, ("-", "dim"))
            ground.append(f"{strategy} {mark}{f' {best:.2f}' if result == 'success' else ''}  ", style=style)
        grid.add_row("GROUND", ground)
    verify = Text(state.verification or "-", style={"success": "green", "failed": "red", "unknown": "yellow"}.get(state.verification, "white"))
    verify.append(f"     recovery {state.recoveries}/{state.max_recoveries}", style="dim")
    if state.recovery_reason:
        verify.append(f" · {state.recovery_reason}", style="dim")
    if state.healed:
        verify.append(f" · {state.healed} healed", style="magenta")
    grid.add_row("VERIFY", verify)
    if state.tools:
        tools = Text()
        for ok, name, seconds in state.tools:
            tools.append(f"{'✓' if ok else '✗'} {name} {seconds:.1f}s  ", style="green" if ok else "red")
        grid.add_row("TOOLS", tools)
    if state.network:
        net = Text("  ·  ".join(state.network), style="dim")
        if state.network_failed:
            net.append(f"  ({state.network_failed} failed)", style="red")
        grid.add_row("NETWORK", net)
    if state.intervention:
        grid.add_row("YOU", Text(state.intervention, style="bold yellow"))
    average = state.action_seconds / state.actions if state.actions else 0.0
    cost = f" · ${state.cost:.4f}" if state.cost is not None else ""
    grid.add_row("COST", Text(f"{state.tokens:,} tokens{cost} · {state.actions} action(s) · avg {average:.2f}s · runtime {state.runtime}", style="dim"))
    notes: list[RenderableType] = [grid]
    for warning in state.warnings:
        notes.append(Text(f"⚠ {warning}", style="yellow"))
    for error in state.errors:
        notes.append(Text(f"✗ {error}", style="red"))
    if state.summary and state.status != "running":
        notes.append(Text(state.summary, style=_status_style(state.status)))
    subtitle = f"trace {state.trace_id}" if state.trace_id else ""
    title = f"[bold magenta]HighhX[/]  [dim]{state.project or '-'} · {state.branch or '-'} · {state.plan_name}{' · ' + state.account if state.account else ''} · {state.connection}[/]"
    return Panel(Group(*notes), title=title, subtitle=subtitle, border_style="magenta", padding=(0, 1))


class LiveDashboard:
    """Shows :func:`render` live while a task runs. ``pause()`` hands the terminal back for an
    approval prompt, and ``resume()`` takes it again."""

    def __init__(self, console: Console, recorder: EventRecorder, state: DashboardState | None = None, *, refresh: float = 8.0) -> None:
        self.console = console
        self.recorder = recorder
        self.state = state or DashboardState()
        self.refresh = refresh
        self._lock = threading.Lock()
        self._live: Live | None = None
        self._unlisten = recorder.listen(self._on)

    def _on(self, record: EventRecord) -> None:
        with self._lock:
            self.state.apply(record)
        if self._live is not None:
            self._live.update(self.renderable())

    def renderable(self) -> RenderableType:
        with self._lock:
            return render(self.state)

    def start(self) -> None:
        from rich.live import Live

        if self._live is None and self.console.is_terminal:
            self._live = Live(self.renderable(), console=self.console, refresh_per_second=self.refresh, transient=False)
            self._live.start()

    def pause(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def resume(self) -> None:
        self.start()

    def stop(self) -> None:
        self.pause()
        self._unlisten()
        self.console.print(self.renderable())

    def __enter__(self) -> LiveDashboard:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


class PausingPrompter:
    """Wraps the terminal prompter so that every approval prompt pauses the live view first:
    the person always sees the full request (risk, reasons, target) before answering."""

    def __init__(self, inner: Any, dashboard: LiveDashboard) -> None:
        self.inner = inner
        self.dashboard = dashboard

    @property
    def interactive(self) -> bool:
        return bool(self.inner.interactive)

    def _ask(self, method: str, *args: Any, **kwargs: Any) -> Any:
        self.dashboard.pause()
        try:
            return getattr(self.inner, method)(*args, **kwargs)
        finally:
            self.dashboard.resume()

    def confirm_action(self, request: Any) -> bool:
        return bool(self._ask("confirm_action", request))

    def ask_permission(self, action: str, details: Any, *, allow_always: bool = True) -> str:
        return str(self._ask("ask_permission", action, details, allow_always=allow_always))

    def confirm(self, message: str, *, default: bool = False) -> bool:
        return bool(self._ask("confirm", message, default=default))

    def confirm_typed(self, message: str, expected: str) -> bool:
        return bool(self._ask("confirm_typed", message, expected))

    def notice(self, level: str, message: str) -> None:
        self.inner.notice(level, message)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)
