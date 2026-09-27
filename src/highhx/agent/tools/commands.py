"""Running things: tests, checks, fixers, builds, dependency installs and arbitrary commands.

All of these go through ``Engine.run`` — risk classification, project policy,
approvals, timeouts, redaction and history apply exactly as for a human user.
"""

from __future__ import annotations

from typing import Any

from highhx.agent.tools.base import Tool, ToolContext, ToolError, ToolResult, truncate
from highhx.approvals.risk import RiskLevel
from highhx.cloud.plans import AGENT_CODE_CHANGES, AGENT_COMMANDS
from highhx.core.result import CommandResult
from highhx.execution.command import CommandSpec
from highhx.safety.actions import ActionKind
from highhx.safety.audit import AuditEvent
from highhx.safety.gate import Authorization
from highhx.utils.validation import Bool, Int, List, Obj, Prop, Str

OUTPUT_TAIL = 400
"""Lines of command output returned to the model."""


def _output(ctx: ToolContext, result: CommandResult | None = None) -> str:
    lines = ctx.output_lines[-OUTPUT_TAIL:]
    text = "\n".join(lines)
    if not text and result is not None:
        text = "\n".join(x for x in (result.stdout, result.stderr) if x)
    return truncate(ctx.app.redactor.redact(text))


def _gate(ctx: ToolContext, tool: str, summary: str, command: str, *, policy_action: str, grant: str) -> Authorization:
    """Classify and approve a command-running tool call through the shared action gate."""
    perms = ctx.permissions
    action = perms.action(ActionKind.EXEC, summary, tool=tool, target=perms.relative(ctx.app.root), command=command)
    return perms.authorize(action, policy_action=policy_action, grant=grant, engine_prompts=True)


def _finish(event: AuditEvent, ok: bool) -> None:
    event.verified = ok
    if not ok:
        event.status = "failed"


def _status(result: CommandResult) -> str:
    if result.dry_run:
        return "dry run (not executed)"
    if result.ok:
        return f"succeeded in {result.duration:.1f}s"
    if str(result.status) == "timeout":
        return f"timed out after {result.duration:.0f}s"
    return f"failed with exit code {result.exit_code}"


class RunCommandTool(Tool):
    name = "run_command"
    label = "Running"
    mutating = True
    feature = AGENT_COMMANDS
    risk = RiskLevel.NORMAL
    description = """
Run a shell command in the project (non-interactive: stdin is closed). Prefer the dedicated
tools (run_tests, run_checks, build, git_*, …) when one fits. Commands are risk-classified
by HighhX: `git push`, `rm -rf`, database drops, production actions and similar need the
user's approval, and project policy may block them. Returns the exit code and output.
"""
    schema = Obj(
        {
            "command": Prop(Str(min_length=1), required=True),
            "cwd": Prop(Str(), description="Working directory relative to the project root."),
            "timeout": Prop(Int(minimum=1, maximum=3600), description="Seconds (default 600)."),
        }
    )

    def describe(self, args: dict[str, Any]) -> str:
        return f"$ {args.get('command')}"

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        command = str(args["command"]).strip()
        cwd = ctx.permissions.resolve(str(args.get("cwd") or "."), must_exist=True)
        engine = ctx.app.engine
        spec = CommandSpec(command, cwd=cwd, timeout=float(args.get("timeout") or 600), name="agent")
        program = spec.program()
        display = ctx.app.redactor.redact(command)
        perms = ctx.permissions
        action = perms.action(
            ActionKind.EXEC, f"Run `{display}`", tool=self.name, target=perms.relative(cwd), command=command
        )
        authorization = perms.authorize(
            action, policy_action="agent:exec", grant=f"exec:{program}", engine_prompts=True
        )
        with perms.executing(authorization) as event:
            result = engine.run(
                spec,
                action=f"Run `{display}`",
                policy_action=f"exec:{program}",
                approved=authorization.ticket is not None,
                cancel=ctx.cancel,
            )
            _finish(event, result.ok or result.dry_run)
        body = f"$ {display}\n{_status(result)}\n\n{_output(ctx, result)}"
        return ToolResult(
            body,
            ok=result.ok,
            summary=_status(result),
            error_code=None if result.ok else ("timeout" if str(result.status) == "timeout" else "failed"),
            verified=True,
        )


class RunTestsTool(Tool):
    name = "run_tests"
    label = "Running tests"
    mutating = True
    risk = RiskLevel.NORMAL
    description = """
Run the project's test suite with the auto-detected runner (pytest, Jest, Vitest, go test,
cargo, Maven, Gradle, Flutter …) or the configured commands.test. Optionally pass extra
runner arguments (e.g. a test file or `-k name`) or run only tests related to changed files.
Returns pass/fail counts and the output tail (failures are at the end).
"""
    schema = Obj(
        {
            "args": Prop(List(Str()), description="Extra arguments for the test runner."),
            "changed_only": Prop(Bool(), description="Only tests related to files changed since HEAD."),
        }
    )

    def describe(self, args: dict[str, Any]) -> str:
        extra = " ".join(args.get("args") or [])
        return f"Run tests {extra}".strip()

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        app = ctx.app
        framework = app.tests.framework()
        authorization = _gate(
            ctx,
            self.name,
            f"Run tests: {framework.command}",
            framework.command,
            policy_action="agent:test",
            grant="test",
        )
        changed = None
        if args.get("changed_only"):
            app.git_repo.require()
            changed = app.git_repo.changed_files(None)
        with ctx.permissions.executing(authorization) as event:
            run = app.tests.run(changed=changed, args=[str(a) for a in args.get("args") or []])
            _finish(event, run.ok or run.result.dry_run or not run.command)
        if not run.command:
            return ToolResult("\n".join(run.notes) or "No tests selected.", summary="no tests selected")
        s = run.summary
        if s.parsed:
            counts = f"{s.passed} passed" + "".join(
                f", {n} {label}"
                for n, label in ((s.failed, "failed"), (s.errors, "errors"), (s.skipped, "skipped"))
                if n
            )
        else:
            counts = f"exit code {run.result.exit_code}"
        failing = s.failed + s.errors
        if run.ok or run.result.dry_run:
            summary = f"Tests passing — {counts}" if not run.result.dry_run else "tests (dry run)"
        elif s.parsed and failing:
            summary = f"{failing} test{'s' if failing != 1 else ''} failing"
        else:
            summary = f"tests failed ({counts})"
        body = f"$ {run.command}\nTests {'passed' if run.ok else 'FAILED'}: {counts}\n\n{_output(ctx, run.result)}"
        return ToolResult(
            body, ok=run.ok, summary=summary, data=run.to_dict(), error_code=None if run.ok else "failed", verified=True
        )


class RunChecksTool(Tool):
    name = "run_checks"
    label = "Running checks"
    mutating = True
    risk = RiskLevel.NORMAL
    description = """
Run the project's quality checks — lint, type-check and (unless include_tests is false)
tests — using the configured or detected commands, and report each result with output.
"""
    schema = Obj({"include_tests": Prop(Bool(), description="Default true.")})

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        app = ctx.app
        commands = app.commands()
        kinds = ["lint", "typecheck"] + (["test"] if args.get("include_tests", True) else [])
        selected = [(k, commands[k]) for k in kinds if commands.get(k)]
        if not selected:
            raise ToolError("No lint, typecheck or test command is configured or detected (see project_overview).")
        joined = " && ".join(c for _k, c in selected)
        authorization = _gate(
            ctx, self.name, f"Run checks: {joined}", joined, policy_action="agent:check", grant="test"
        )
        sections: list[str] = []
        failed: list[str] = []
        with ctx.permissions.executing(authorization) as event, app.engine.operation("check", "agent"):
            for kind, command in selected:
                ctx.output_lines.clear()
                result = app.engine.run(
                    CommandSpec(command, cwd=app.root, name=kind), action=f"{kind}: {command}", policy_action=kind
                )
                if not result.ok and not result.dry_run:
                    failed.append(kind)
                sections.append(f"## {kind}: $ {command}\n{_status(result)}\n{_output(ctx, result)}")
            _finish(event, not failed)
        summary = "all checks passed" if not failed else f"{', '.join(failed)} failing"
        return ToolResult(
            truncate("\n\n".join(sections)),
            ok=not failed,
            summary=summary,
            error_code="failed" if failed else None,
            verified=True,
        )


class RunFixTool(Tool):
    name = "run_fix"
    label = "Applying automatic fixes"
    mutating = True
    feature = AGENT_CODE_CHANGES
    risk = RiskLevel.NORMAL
    description = """
Run the project's automatic fixers: commands.fix (e.g. `ruff check --fix`, `eslint --fix`)
then commands.format (e.g. `ruff format`, `prettier --write`). These rewrite files.
"""

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        app = ctx.app
        commands = app.commands()
        selected = [(k, commands[k]) for k in ("fix", "format") if commands.get(k)]
        if not selected:
            raise ToolError("No fix or format command is configured or detected.")
        joined = " && ".join(c for _k, c in selected)
        authorization = _gate(
            ctx, self.name, f"Apply automatic fixes: {joined}", joined, policy_action="agent:fix", grant="edit"
        )
        lines: list[str] = []
        ok = True
        with ctx.permissions.executing(authorization) as event, app.engine.operation("fix", "agent"):
            for kind, command in selected:
                ctx.output_lines.clear()
                result = app.engine.run(
                    CommandSpec(command, cwd=app.root, name=kind), action=f"{kind}: {command}", policy_action=kind
                )
                lines.append(f"$ {command}\n{_status(result)}\n{_output(ctx, result)}")
                if not result.ok and not result.dry_run:
                    ok = False
                    break
            _finish(event, ok)
        return ToolResult(
            "\n\n".join(lines),
            ok=ok,
            summary="fixes applied" if ok else "fixer failed",
            error_code=None if ok else "failed",
        )


class BuildTool(Tool):
    name = "build"
    label = "Building"
    mutating = True
    risk = RiskLevel.NORMAL
    description = "Build the project with commands.build (configured or detected) and list new artifacts."
    schema = Obj({"package": Prop(Bool(), description="Run commands.package instead of build.")})

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        kind = "package" if args.get("package") else "build"
        command = ctx.app.builder.command_for(kind)
        authorization = _gate(
            ctx, self.name, f"{kind.capitalize()}: {command}", command, policy_action="agent:build", grant="build"
        )
        with ctx.permissions.executing(authorization) as event:
            outcome = ctx.app.builder.package() if kind == "package" else ctx.app.builder.build()
            _finish(event, outcome.result.ok or outcome.result.dry_run)
        body = f"$ {outcome.command}\n{_status(outcome.result)}\nnew artifacts: {outcome.new_artifacts or 'none'}\n\n"
        body += _output(ctx, outcome.result)
        return ToolResult(
            body,
            ok=outcome.result.ok,
            summary=f"{kind} {_status(outcome.result)}",
            data=outcome.to_dict(),
            error_code=None if outcome.result.ok else "failed",
            verified=True,
        )


class InstallDependenciesTool(Tool):
    name = "install_dependencies"
    label = "Installing dependencies"
    mutating = True
    feature = AGENT_COMMANDS
    risk = RiskLevel.NORMAL
    description = "Install the project's dependencies with its package manager(s) (pip/uv/poetry, npm/pnpm/yarn …)."

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        commands = (
            " && ".join(str(a.get("install") or a.get("manager")) for a in ctx.app.dependencies.summary()) or "install"
        )
        authorization = _gate(
            ctx, self.name, "Install the project's dependencies", commands, policy_action="agent:deps", grant="deps"
        )
        with ctx.permissions.executing(authorization) as event:
            results = ctx.app.dependencies.install()
            ok = all(r.ok or r.dry_run for r in results)
            _finish(event, ok)
        body = "\n".join(f"$ {r.command}: {_status(r)}" for r in results) + "\n\n" + _output(ctx)
        return ToolResult(body, ok=ok, summary="dependencies installed" if ok else "install failed")
