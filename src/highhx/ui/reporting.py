"""Terminal reporter for workflow execution."""

from __future__ import annotations

from rich.markup import escape

from highhx.core.result import Status, StepResult, WorkflowResult
from highhx.ui.output import Output
from highhx.utils.time import format_duration
from highhx.workflows.schema import StepSpec, WorkflowSpec


class WorkflowConsoleReporter:
    """Prints step lifecycle lines; silent in JSON/quiet mode."""

    def __init__(self, out: Output) -> None:
        self.out = out

    def _indent(self, depth: int) -> str:
        return "  " * depth

    def workflow_started(self, spec: WorkflowSpec, stages: list[list[str]], depth: int) -> None:
        if depth == 0:
            self.out.markup(
                f"[title]▶ {escape(spec.name)}[/title] [muted]({len(spec.steps)} steps, {len(stages)} stages)[/muted]"
            )
        else:
            self.out.markup(f"{self._indent(depth)}[muted]↳ workflow {escape(spec.name)}[/muted]")

    def step_started(self, spec: WorkflowSpec, step: StepSpec, depth: int) -> None:
        self.out.markup(f"{self._indent(depth)}[info]{self.out.symbols.arrow}[/info] {escape(step.label)}")

    def step_finished(self, spec: WorkflowSpec, result: StepResult, depth: int) -> None:
        status = str(result.status)
        suffix = ""
        if result.status in (Status.SUCCESS, Status.FAILED, Status.TIMEOUT):
            suffix = f" [muted]({format_duration(result.duration)})[/muted]"
        message = f" — {escape(result.message)}" if result.message and result.status != Status.SUCCESS else ""
        allowed = " [warn](allowed to fail)[/warn]" if result.allowed_failure else ""
        self.out.markup(
            f"{self._indent(depth)}{self.out.status_symbol(status)} {escape(result.step_id)}{suffix}{message}{allowed}"
        )

    def workflow_finished(self, result: WorkflowResult, depth: int) -> None:
        if depth:
            return
        counts: dict[str, int] = {}
        for step in result.steps.values():
            counts[str(step.status)] = counts.get(str(step.status), 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in counts.items())
        symbol = self.out.status_symbol(str(result.status))
        self.out.markup(
            f"\n{symbol} workflow [title]{escape(result.workflow)}[/title] {escape(str(result.status))} in {format_duration(result.duration)} [muted]({escape(summary)})[/muted]"
        )
        if result.outputs:
            self.out.kv(result.outputs, title="Outputs")
        self.out.note(f"execution id: {result.execution_id}")

    def plan(self, spec: WorkflowSpec, stages: list[list[str]], commands: dict[str, list[str]]) -> None:
        self.out.markup(f"[title]Dry run: {escape(spec.name)}[/title] — nothing will be executed\n")
        for index, stage in enumerate(stages, start=1):
            self.out.markup(f"[muted]Stage {index}{' (parallel)' if len(stage) > 1 else ''}[/muted]")
            for step_id in stage:
                step = spec.step(step_id)
                flags = []
                if step.depends_on:
                    flags.append(f"after {', '.join(step.depends_on)}")
                if step.condition:
                    flags.append(f"if {step.condition}")
                if step.approval:
                    flags.append(f"approval ({step.approval.risk.label})")
                if step.retry.attempts > 1:
                    flags.append(f"retry x{step.retry.attempts}")
                if step.timeout:
                    flags.append(f"timeout {format_duration(step.timeout)}")
                if step.continue_on_error:
                    flags.append("continue on error")
                extra = f" [muted]({escape('; '.join(flags))})[/muted]" if flags else ""
                self.out.markup(f"  {self.out.symbols.bullet} {escape(step.label)}{extra}")
                for command in commands.get(step_id, []):
                    self.out.markup(f"      [muted]$[/muted] {escape(command)}")
