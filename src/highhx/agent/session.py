"""The agent session: the loop that turns a request into verified work.

    user request → model (streamed) → tool calls → HighhX tools (policy, risk,
    approval, execution, history) → results → model → … → final answer

One user turn is recorded as one ``agent`` entry in ``highhx history`` (each
command it ran is a step), the transcript is saved for ``--resume``, and the
session's metadata and usage are synced to the HighhX platform.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from highhx.agent.context import ProjectContext
from highhx.agent.history import SessionBusyError, SessionRecord, SessionStore, lease_owner
from highhx.agent.memory import ProjectMemory
from highhx.agent.messages import ImageBlock, Message, TextBlock, ToolCall, ToolResultBlock, Usage, limit_images, without_images
from highhx.agent.model.base import ModelProvider, ModelRequest
from highhx.agent.model.resilience import CircuitBreaker, RetryPolicy
from highhx.agent.permissions import AgentPermissions, ApprovalMode
from highhx.agent.planner import Plan
from highhx.agent.prompts import system_prompt
from highhx.agent.settings import AgentSettings
from highhx.agent.state import SessionState, check_transition, parse_state
from highhx.agent.streaming import Completed, TextDelta, ToolCallStarted
from highhx.agent.tools.base import Tool, ToolContext, ToolError, ToolResult
from highhx.agent.tools.files import ChangeJournal
from highhx.agent.tools.registry import ToolRegistry
from highhx.core.errors import (
    ApprovalDeniedError,
    HighhXError,
    ModelProviderError,
    OperationCancelledError,
    PolicyViolationError,
)
from highhx.execution.cancellation import CancellationToken
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.confirmation import ConfirmationRequest
from highhx.safety.injection import frame_untrusted

if TYPE_CHECKING:
    from highhx.agent.sync import SessionSync
    from highhx.commands import App
    from highhx.computer.session import ComputerSession

log = logging.getLogger(__name__)

MAX_PAUSES = 3


class AgentUI(Protocol):
    """Everything the session shows or asks. Implemented by the terminal UI and by test doubles."""

    @property
    def interactive(self) -> bool: ...

    def assistant_started(self) -> None: ...

    def assistant_text(self, delta: str) -> None: ...

    def assistant_finished(self) -> None: ...

    def tool_started(self, tool: Tool | None, call: ToolCall, description: str) -> None: ...

    def tool_output(self, line: str) -> None: ...

    def tool_finished(self, tool: Tool | None, call: ToolCall, result: ToolResult, seconds: float) -> None: ...

    def present_plan(self, plan: Plan) -> tuple[bool, str]: ...

    def plan_updated(self, plan: Plan, index: int) -> None: ...

    def ask_permission(self, action: str, details: Sequence[str], *, allow_always: bool = True) -> str: ...

    def confirm(self, message: str, *, default: bool = False) -> bool: ...

    def confirm_typed(self, message: str, expected: str) -> bool: ...

    def confirm_action(self, request: ConfirmationRequest) -> bool: ...

    def notice(self, level: str, message: str) -> None: ...


class EngineSink:
    """Receives the engine's output while the agent runs tools (instead of printing it raw)."""

    def __init__(self, ui: AgentUI) -> None:
        self.ui = ui
        self.buffer: list[str] | None = None

    def stream_line(self, line: str, *, stream: str = "stdout", source: str | None = None) -> None:
        if self.buffer is not None:
            self.buffer.append(line)
        self.ui.tool_output(line)

    def info(self, message: str) -> None:
        self.ui.notice("info", message)

    def warn(self, message: str) -> None:
        self.ui.notice("warn", message)

    def detail(self, message: str) -> None:
        return None


class _ConfirmingUI:
    """Wraps the UI so the session is ``waiting_for_confirmation`` while a person decides."""

    def __init__(self, ui: AgentUI, session: AgentSession) -> None:
        self._ui = ui
        self._session = session

    @property
    def interactive(self) -> bool:
        return self._ui.interactive

    def _waiting(self, ask: Callable[[], Any]) -> Any:
        session = self._session
        if session.state != SessionState.RUNNING:
            return ask()
        session.transition(SessionState.WAITING_FOR_CONFIRMATION)
        try:
            return ask()
        finally:
            if session.state == SessionState.WAITING_FOR_CONFIRMATION:
                session.transition(SessionState.RUNNING)

    def ask_permission(self, action: str, details: Sequence[str], *, allow_always: bool = True) -> str:
        return str(self._waiting(lambda: self._ui.ask_permission(action, details, allow_always=allow_always)))

    def confirm_action(self, request: ConfirmationRequest) -> bool:
        return bool(self._waiting(lambda: self._ui.confirm_action(request)))

    def confirm(self, message: str, *, default: bool = False) -> bool:
        return bool(self._waiting(lambda: self._ui.confirm(message, default=default)))

    def confirm_typed(self, message: str, expected: str) -> bool:
        return bool(self._waiting(lambda: self._ui.confirm_typed(message, expected)))


class UIPrompter:
    """The engine's approval prompter, routed through the agent UI (so prompts never collide
    with live progress output)."""

    def __init__(self, ui: AgentUI | _ConfirmingUI) -> None:
        self.ui = ui

    @property
    def interactive(self) -> bool:
        return self.ui.interactive

    def confirm(self, message: str, *, default: bool = False) -> bool:
        return self.ui.confirm(message, default=default)

    def confirm_typed(self, message: str, expected: str) -> bool:
        return self.ui.confirm_typed(message, expected)


@dataclass
class TurnResult:
    text: str
    steps: int = 0
    tools: list[tuple[str, bool]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    changed_files: list[str] = field(default_factory=list)
    seconds: float = 0.0
    stopped: str = "completed"
    """completed | max_steps | refusal | max_tokens | cancelled"""


class AgentSession:
    def __init__(
        self,
        app: App,
        provider: ModelProvider,
        settings: AgentSettings,
        ui: AgentUI,
        *,
        features: frozenset[str],
        context: ProjectContext,
        memory: ProjectMemory,
        store: SessionStore | None = None,
        record: SessionRecord | None = None,
        sync: SessionSync | None = None,
        registry: ToolRegistry | None = None,
        sleep: Callable[[float], None] = time.sleep,
        account_id: str | None = None,
    ) -> None:
        self.app = app
        self.provider = provider
        self.settings = settings
        self.ui = ui
        self.features = features
        self.context = context
        self.memory = memory
        self.store = store
        self.record = record
        self.sync = sync
        self._sleep = sleep
        self.messages: list[Message] = []
        self.plan: Plan | None = None
        self.usage = Usage()
        self.journal = ChangeJournal()
        self.turns = 0
        self.permissions = AgentPermissions(
            app,
            _ConfirmingUI(ui, self),
            mode=settings.approval,
            assume_yes=app.options.yes,
            audit=AuditLog(app.db, app.redactor) if app.db is not None else None,
            session_id=record.id if record else None,
            account_id=account_id,
        )
        self.mcp: Any = None
        """External MCP servers (:class:`~highhx.integrations.mcp_client.McpManager`), when configured."""
        self.registry = registry or self._build_registry()
        self.sink = EngineSink(ui)
        # Route the engine's output and approval prompts through the agent UI.
        app.engine.output = self.sink
        app.approvals.prompter = UIPrompter(_ConfirmingUI(ui, self))
        self.system = self._system_prompt()
        self.cancel = CancellationToken()
        self._computer: ComputerSession | None = None
        self.retry = RetryPolicy()
        self.breaker = CircuitBreaker()
        self.state = parse_state(record.status) if record is not None else SessionState.CREATED
        self._turn_lock = threading.Lock()
        self._lease: str | None = None
        from highhx.agent.model.capabilities import capabilities_for

        self.capabilities = capabilities_for(settings.provider, settings.model)
        """What the model takes (images?) — tools send screenshots only to a model that can see them."""
        self._images: list[ImageBlock | TextBlock] = []
        from highhx.attachments import AttachmentStore

        self.attachments = AttachmentStore()
        """Files attached in this conversation (``@path`` in a request, or /attach)."""

    # ------------------------------------------------------------------ setup
    def _build_registry(self) -> ToolRegistry:
        from highhx.agent.tools.actions import RunActionsTool

        return ToolRegistry.for_session(
            features=self.features,
            read_only=self.settings.approval == ApprovalMode.READ_ONLY,
            initialized=self.app.initialized,
            extra=[RunActionsTool(self.features), *self._mcp_tools()],
        )

    def _mcp_tools(self) -> list[Any]:
        """Tools of the configured external MCP servers; a server that fails is said, not fatal."""
        from highhx.agent.tools.mcp import mcp_tools
        from highhx.integrations.mcp_client import McpError, McpManager, load_servers

        try:
            servers = load_servers(self.app.config.raw if self.app.initialized else None)
        except McpError as exc:
            self.ui.notice("warn", exc.message)
            return []
        if not servers:
            return []
        self.mcp = McpManager(servers)
        for problem in self.mcp.connect_all():
            self.ui.notice("warn", problem)
        self.app.ctx.events.emit("mcp.connected", servers=self.mcp.status())
        return mcp_tools(self.mcp)

    def _system_prompt(self) -> str:
        return system_prompt(
            self.context,
            mode=self.settings.approval,
            memory=self.memory.facts(),
            extra_instructions=self.settings.instructions,
        )

    def set_mode(self, mode: ApprovalMode) -> None:
        self.settings.approval = mode
        self.permissions.mode = mode
        self.registry = self._build_registry()
        self.system = self._system_prompt()

    def set_provider(self, provider: ModelProvider, model: str | None, *, choice: str | None = None) -> None:
        self.provider = provider
        self.settings.provider = choice or provider.name
        self.settings.model = model
        if self.record is not None:
            self.record.provider, self.record.model = self.settings.provider, model

    @property
    def model_label(self) -> str:
        return self.settings.model or self.provider.default_model

    def load_transcript(self, messages: list[Message]) -> None:
        self.messages = list(messages)
        self._repair_transcript()
        self.turns = sum(1 for m in self.messages if m.role == "user" and not m.tool_results)

    def clear(self) -> None:
        """Forget the conversation (keeps settings, memory, grants and the change journal)."""
        self.messages.clear()
        self.plan = None
        self.system = self._system_prompt()

    # ------------------------------------------------------------ persistence
    def _persist(self, message: Message) -> None:
        self.messages.append(message)
        if self.store is not None and self.record is not None:
            try:
                self.store.append(self.record.id, without_images(message))
            except Exception as exc:  # storage must never break the session
                log.warning("could not save agent message: %s", exc)

    def transition(self, target: SessionState) -> None:
        """Move the session to ``target`` (validated) and persist it."""
        check_transition(self.state, target)
        if target == self.state:
            return
        self.state = target
        self._save_record()

    def open(self) -> None:
        """Attach this process to the session (one process at a time)."""
        if self.store is not None and self.record is not None and self._lease is None:
            owner = lease_owner()
            self.store.acquire(self.record.id, owner)
            self._lease = owner

    def _save_record(self) -> None:
        if self.record is None:
            return
        self.record.status = str(self.state)
        self.record.turns = self.turns
        self.record.usage = self.usage
        self.record.plan = self.plan
        if self.store is not None:
            try:
                self.store.update(self.record)
            except Exception as exc:
                log.warning("could not save agent session: %s", exc)
        if self.sync is not None:
            self.sync.update(self.record)

    def _request_message(self, text: str) -> Message:
        """The person's request, with the files it mentions (``@report.pdf``) or that were attached
        before it — their text, and pictures when the model takes images."""
        from highhx.attachments import context_blocks

        _added, problems = self.attachments.mentions(text, self.app.start_dir)
        for problem in problems:
            self.ui.notice("warn", problem)
        attached = self.attachments.take_pending()
        if not attached:
            return Message.user(text)
        self.app.ctx.events.emit(
            "attachments.added", files=[a.to_dict() for a in attached], vision=self.capabilities.vision
        )
        return Message("user", [TextBlock(text), *context_blocks(attached, vision=self.capabilities.vision)])

    def _repair_transcript(self) -> None:
        """Answer tool calls left unanswered by an interrupted turn (providers reject dangling calls)."""
        if self.messages and self.messages[-1].role == "assistant" and self.messages[-1].tool_calls:
            results: list[Any] = [
                ToolResultBlock(call.id, "Interrupted by the user before this ran.", True, call.name)
                for call in self.messages[-1].tool_calls
            ]
            self._persist(Message("user", results))

    # ------------------------------------------------------------------- turn
    def run_turn(self, text: str, *, cancel: CancellationToken | None = None) -> TurnResult:
        """Handle one user request to completion (or until cancelled / out of steps)."""
        if not self._turn_lock.acquire(blocking=False):
            raise SessionBusyError("This agent session is already handling a request.")
        try:
            self.open()
            self.transition(SessionState.RUNNING)
            return self._run_turn(text, cancel)
        finally:
            self._turn_lock.release()

    def _run_turn(self, text: str, cancel: CancellationToken | None) -> TurnResult:
        self.cancel = cancel or CancellationToken()
        # Commands started through the engine during this turn observe the turn's token.
        self.app.ctx.cancel = self.cancel
        started = time.monotonic()
        self._repair_transcript()
        self.turns += 1
        if self.record is not None and self.turns == 1 and self.record.title in ("", "New session"):
            self.record.title = " ".join(text.split())[:80]
        if self.sync is not None and self.record is not None:
            self.sync.start(self.record, self.context)
        self.journal.begin_turn()
        self._persist(self._request_message(text))
        self.app.ctx.events.emit(
            "agent.turn", session=self.record.id if self.record else None, turn=self.turns, text=text[:200]
        )
        result = TurnResult(text="")
        usage_before = Usage(**self.usage.to_dict())
        outcome = SessionState.FAILED
        title = " ".join(text.split())[:60]
        try:
            with self.app.engine.operation("agent", title, metadata={"provider": self.provider.name}) as op:
                self._loop(result)
                if result.stopped not in ("completed",):
                    op.metadata["stopped"] = result.stopped
            outcome = SessionState.COMPLETED
        except (KeyboardInterrupt, OperationCancelledError):
            result.stopped = "cancelled"
            self._repair_transcript()
            outcome = SessionState.CANCELLED
        finally:
            result.seconds = time.monotonic() - started
            result.usage = Usage(
                self.usage.input_tokens - usage_before.input_tokens,
                self.usage.output_tokens - usage_before.output_tokens,
                self.usage.cache_read_tokens - usage_before.cache_read_tokens,
                self.usage.cache_write_tokens - usage_before.cache_write_tokens,
            )
            turn = self.journal.turns[-1] if self.journal.turns else []
            result.changed_files = list(dict.fromkeys(self.permissions.relative(c.path) for c in turn))
            self.app.ctx.events.emit(
                "agent.completed",
                session=self.record.id if self.record else None,
                stopped=result.stopped,
                steps=result.steps,
                changed=len(result.changed_files),
                seconds=round(result.seconds, 2),
            )
            if self.state == SessionState.WAITING_FOR_CONFIRMATION:
                self.state = SessionState.RUNNING
            self.transition(outcome)
        return result

    def _loop(self, result: TurnResult) -> None:
        pauses = 0
        while True:
            if self.cancel.cancelled:
                raise OperationCancelledError("Turn cancelled.")
            if result.steps >= self.settings.max_steps:
                result.stopped = "max_steps"
                self.ui.notice(
                    "warn",
                    f"Stopped after {result.steps} tool steps (limit {self.settings.max_steps}). "
                    "Say 'continue' to keep going.",
                )
                return
            completed = self._call_model()
            message = completed.message
            self.usage.add(completed.usage)
            self._persist(message)
            if message.text:
                result.text = message.text
            if completed.stop_reason == "refusal":
                result.stopped = "refusal"
                self.ui.notice("warn", "The model declined this request.")
                return
            calls = message.tool_calls
            if not calls:
                if completed.stop_reason == "pause" and pauses < MAX_PAUSES:
                    pauses += 1
                    continue
                if completed.stop_reason == "max_tokens":
                    result.stopped = "max_tokens"
                    self.ui.notice("warn", "The response hit the output limit. Say 'continue' to resume.")
                return
            if completed.stop_reason == "max_tokens":
                # Tool input was cut off; a truncated input can still parse, so never run it.
                blocks: list[Any] = [
                    ToolResultBlock(
                        c.id,
                        "Not run: your response hit the output limit, so this tool input may be truncated. "
                        "Retry with smaller inputs (e.g. several edit_file calls instead of one large write).",
                        True,
                        c.name,
                    )
                    for c in calls
                ]
                self._persist(Message("user", blocks))
                result.steps += 1
                continue
            outcomes: list[Any] = []
            self._images = []
            for call in calls:
                block = self._execute(call, result)
                outcomes.append(block)
            self._persist(Message("user", [*outcomes, *self._images]))  # images after every tool result
            result.steps += 1

    def _call_model(self) -> Completed:
        request = ModelRequest(
            system=self.system,
            messages=limit_images(self.messages),
            tools=self.registry.specs(),
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            effort=self.settings.effort,
            session_id=self.record.remote_id if self.record else None,
        )
        attempt = 0
        while True:
            self.breaker.check()
            completed: Completed | None = None
            # A fresh idempotency key per attempt: the platform meters each attempt exactly once.
            request.attempt_id = uuid.uuid4().hex
            self.ui.assistant_started()
            try:
                for event in self.provider.stream(request, cancel=self.cancel):
                    if isinstance(event, TextDelta):
                        self.ui.assistant_text(event.text)
                    elif isinstance(event, ToolCallStarted):
                        continue
                    elif isinstance(event, Completed):
                        completed = event
                if completed is None:
                    raise ModelProviderError("The model stream ended without a response.", retryable=True)
            except ModelProviderError as exc:
                self.ui.assistant_finished()
                self.breaker.failure()
                attempt += 1
                if (
                    exc.retryable
                    and attempt < self.retry.max_attempts
                    and not self.cancel.cancelled
                    and not self.breaker.open
                ):
                    delay = self.retry.delay(attempt)
                    self.ui.notice(
                        "warn", f"{exc.message} Retrying in {delay:.0f}s ({attempt}/{self.retry.max_attempts - 1}) …"
                    )
                    if self.cancel.wait(delay):
                        raise OperationCancelledError("Turn cancelled.") from None
                    continue
                raise
            self.ui.assistant_finished()
            self.breaker.success()
            return completed

    # ------------------------------------------------------------------ tools
    def _tool_context(self) -> ToolContext:
        return ToolContext(
            app=self.app,
            permissions=self.permissions,
            cancel=self.cancel.child(),
            host=self,
            remember=self.memory.add,
            journal=self.journal,
            computer=self.computer_session,
        )

    def _execute(self, call: ToolCall, result: TurnResult) -> ToolResultBlock:
        tool = self.registry.get(call.name)
        if tool is None:
            # Only registered tools exist for the model; anything else is refused (no hidden tools).
            outcome = ToolResult.error(
                f"Unknown tool '{call.name}'. Available: {', '.join(self.registry.names())}", code="unknown_tool"
            )
            self.ui.tool_finished(None, call, outcome, 0.0)
            result.tools.append((call.name, False))
            return ToolResultBlock(call.id, outcome.content, True, call.name)
        problems = tool.validate(call.input)
        if problems:
            outcome = ToolResult.error(
                "Invalid tool input:\n" + "\n".join(f"- {p}" for p in problems), code="invalid_input"
            )
            self.ui.tool_finished(tool, call, outcome, 0.0)
            result.tools.append((call.name, False))
            return ToolResultBlock(call.id, outcome.content, True, call.name)
        ctx = self._tool_context()
        timer = threading.Timer(tool.timeout, lambda: ctx.cancel.cancel("timeout"))
        timer.daemon = True
        self.sink.buffer = ctx.output_lines
        # Everything the tool starts through the engine observes the tool's own token.
        self.app.ctx.cancel = ctx.cancel
        self.ui.tool_started(tool, call, tool.describe(call.input))
        started = time.monotonic()
        timer.start()
        try:
            outcome = tool.run(ctx, call.input)
        except (KeyboardInterrupt, OperationCancelledError):
            if self.cancel.cancelled or ctx.cancel.reason != "timeout":
                raise
            outcome = ToolResult.error(f"{call.name} timed out after {tool.timeout:.0f}s.", code="timeout")
        except ToolError as exc:
            outcome = ToolResult.error(str(exc), code="invalid_input")
        except ApprovalDeniedError as exc:
            outcome = ToolResult.error(
                f"{exc.message}. The user did not approve this action — do not retry it unchanged; "
                "adjust your approach or ask the user.",
                summary="declined",
                code="denied",
            )
        except PolicyViolationError as exc:
            reasons = "; ".join(exc.details)
            outcome = ToolResult.error(
                f"{exc.message}{f' ({reasons})' if reasons else ''}. The safety policy forbids this; "
                "do not try to work around it.",
                summary="blocked by policy",
                code="policy",
            )
        except HighhXError as exc:
            detail = "\n".join(exc.details[-20:])
            outcome = ToolResult.error(
                exc.message + (f"\n{detail}" if detail else "") + (f"\nHint: {exc.hint}" if exc.hint else ""),
                code="not_found" if exc.category == "not_found" else "failed",
            )
        except Exception as exc:
            log.exception("tool %s failed", call.name)
            outcome = ToolResult.error(f"Internal error in {call.name}: {type(exc).__name__}: {exc}", code="internal")
        finally:
            timer.cancel()
            self.sink.buffer = None
            self.app.ctx.cancel = self.cancel
        if ctx.cancel.reason == "timeout" and outcome.error_code != "timeout" and not self.cancel.cancelled:
            outcome = ToolResult.error(
                f"{call.name} timed out after {tool.timeout:.0f}s.\n{outcome.content}", code="timeout"
            )
        seconds = time.monotonic() - started
        self.app.ctx.events.emit(
            "agent.tool_call",
            session=self.record.id if self.record else None,
            tool=call.name,
            ok=outcome.ok,
            error_code=outcome.error_code,
            seconds=round(seconds, 2),
        )
        self.ui.tool_finished(tool, call, outcome, seconds)
        result.tools.append((call.name, outcome.ok))
        content = self.app.redactor.redact(outcome.content)
        if tool.untrusted_output:
            content = frame_untrusted(content, source=f"the {call.name} tool")
        for image in outcome.images:
            if self.capabilities.vision:
                self._images.append(image)
            else:
                self._images.append(TextBlock(f"[not shown — this model does not take images: {image.label}]"))
        return ToolResultBlock(call.id, content, not outcome.ok, call.name)

    def computer_session(self) -> ComputerSession:
        if self._computer is None:
            from highhx.computer.session import ComputerSession

            from highhx.connections import browser_endpoint, computer_target

            self._computer = ComputerSession(
                self.permissions.gate,
                actor=Actor.AGENT,
                tool="computer",
                cancel=self.cancel,
                target=computer_target(self.app),
                browser_endpoint=browser_endpoint(self.app),
            )
        return self._computer

    # ----------------------------------------------------------- plan host
    def present_plan(self, plan: Plan) -> tuple[bool, str]:
        return self.ui.present_plan(plan)

    def plan_updated(self, plan: Plan, index: int) -> None:
        self.ui.plan_updated(plan, index)
        self._save_record()

    # --------------------------------------------------------------- misc
    def note(self, text: str) -> None:
        """Add a note from HighhX to the conversation (seen by the model on the next turn)."""
        self._persist(Message.user(f"[HighhX note] {text}"))

    def undo(self) -> list[str]:
        restored = self.journal.undo_last()
        return [self.permissions.relative(p) for p in restored]

    def close(self) -> None:
        if self._computer is not None:
            self._computer.close()
        if self.mcp is not None:
            self.mcp.close()
        if self.state in (SessionState.RUNNING, SessionState.WAITING_FOR_CONFIRMATION):
            self.state = SessionState.CANCELLED
        self.transition(SessionState.CLOSED)
        if self._lease is not None and self.store is not None and self.record is not None:
            self.store.release(self.record.id, self._lease)
            self._lease = None
