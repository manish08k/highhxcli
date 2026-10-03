"""``sandbox.*`` handlers: create, run in, inspect, apply and destroy sandboxes (after the executor
approved the action). A command run in a sandbox is classified like any other command: the
sandbox limits what it can reach and never lowers its risk."""

from __future__ import annotations

import shlex
import tempfile
from pathlib import Path
from typing import Any

from highhx.actions import events as ev
from highhx.actions.spec import ActionContext, ActionResult, Inputs
from highhx.agent.tools.base import ToolError
from highhx.core.errors import HighhXError
from highhx.runtimes.base import ResourceLimits
from highhx.runtimes.sandbox import SandboxManager


def manager(ctx: ActionContext) -> SandboxManager:
    return SandboxManager()


def _argv(inputs: Inputs) -> list[str]:
    if inputs.get("argv"):
        return [str(a) for a in inputs["argv"]]
    command = str(inputs.get("command") or "")
    if not command:
        raise ToolError("give a command or argv")
    return ["/bin/sh", "-c", command]


def command_of(inputs: Inputs) -> str:
    return shlex.join([str(a) for a in inputs["argv"]]) if inputs.get("argv") else str(inputs.get("command") or "")


def create(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    defaults = ResourceLimits()
    limits = ResourceLimits(
        timeout=float(inputs.get("timeout") or defaults.timeout),
        cpu_seconds=int(inputs.get("cpu_seconds") or defaults.cpu_seconds),
        file_mb=int(inputs.get("file_mb") or defaults.file_mb),
        memory_mb=int(inputs.get("memory_mb") or 0),
        processes=int(inputs.get("processes") or 0),
    )
    try:
        runtime = manager(ctx).create(
            ctx.app.root,
            isolation=inputs.get("isolation"),
            network=str(inputs.get("network") or "deny"),
            limits=limits,
            copy=inputs.get("copy", True) is not False,
        )
    except HighhXError as exc:
        raise ToolError(exc.message + (f" ({exc.hint})" if exc.hint else "")) from None
    info = runtime.info().to_dict()
    ctx.app.ctx.events.emit(ev.SANDBOX_CREATED, sandbox=runtime.id, isolation=info["isolation"], network=info["network"])
    ctx.app.ctx.events.emit(ev.RUNTIME_STARTED, runtime="sandbox", id=runtime.id)
    return ActionResult(True, output=info, summary=f"sandbox {runtime.id} ({info['isolation']}, network {info['network']})", verified=Path(info["workspace"]).is_dir())


def list_(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    found = [r.info().to_dict() for r in manager(ctx).all()]
    return ActionResult(True, output={"sandboxes": found}, summary=f"{len(found)} sandbox(es)")


def exec_(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    runtime = manager(ctx).get(str(inputs["id"]))
    result = runtime.exec(_argv(inputs), timeout=float(inputs["timeout"]) if inputs.get("timeout") else None, cancel=ctx.cancel)
    ctx.app.ctx.events.emit(ev.SANDBOX_EXEC, sandbox=runtime.id, exit_code=result.exit_code, seconds=round(result.seconds, 3), timed_out=result.timed_out)
    status = "timeout" if result.timed_out else ("cancelled" if result.cancelled else ("ok" if result.ok else "failed"))
    detail = (result.stderr or result.stdout).strip().splitlines()[-1:] if not result.ok else []
    return ActionResult(
        result.ok,
        output=result.to_dict() | {"sandbox": runtime.id},
        summary=f"exit {result.exit_code} in sandbox {runtime.id}" + (" (timed out)" if result.timed_out else ""),
        status=status if status in ("timeout", "cancelled") else "",
        verified=result.ok,
        error="" if result.ok else (detail[0] if detail else f"exit code {result.exit_code}"),
        retryable=False,
    )


def patch(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    runtime = manager(ctx).get(str(inputs["id"]))
    diff = runtime.patch()
    files = sorted({line[6:] for line in diff.splitlines() if line.startswith("+++ b/")} | {line[6:] for line in diff.splitlines() if line.startswith("--- a/")})
    return ActionResult(True, output={"patch": diff, "files": files}, summary=f"{len(files)} file(s) changed in sandbox {runtime.id}")


def apply_preview(app: Any, inputs: Inputs) -> list[str]:
    diff = SandboxManager().get(str(inputs["id"])).patch()
    return [line for line in diff.splitlines() if line.startswith(("+++ ", "--- "))][:40]


def apply(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    from highhx.execution.command import CommandSpec

    runtime = manager(ctx).get(str(inputs["id"]))
    diff = runtime.patch()
    if not diff.strip():
        return ActionResult(True, output={"files": []}, summary="nothing to apply", verified=True)
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as handle:
        handle.write(diff)
    path = Path(handle.name)
    try:
        for argv in (["git", "apply", "--check", str(path)], ["git", "apply", str(path)]):
            result = ctx.app.engine.run(CommandSpec(argv, name="git apply", cwd=ctx.app.root, timeout=120), action="apply the sandbox patch", approved=True, echo=False, record=False, policy_action="sandbox:apply")
            if not result.ok:
                return ActionResult(False, error=(result.stderr or result.error or "git apply failed").strip(), summary="the patch does not apply", retryable=False)
    finally:
        path.unlink(missing_ok=True)
    files = sorted({line[6:] for line in diff.splitlines() if line.startswith("+++ b/")})
    return ActionResult(True, output={"files": files}, changed=files, summary=f"applied {len(files)} file(s) from sandbox {runtime.id}", verified=True)


def destroy(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    sandboxes = manager(ctx)
    ids = [r.id for r in sandboxes.all()] if inputs.get("all") else [str(inputs.get("id") or "")]
    if not ids or ids == [""]:
        raise ToolError("give a sandbox id, or all")
    for sandbox_id in ids:
        sandboxes.destroy(sandbox_id)
        ctx.app.ctx.events.emit(ev.SANDBOX_DESTROYED, sandbox=sandbox_id)
        ctx.app.ctx.events.emit(ev.RUNTIME_STOPPED, runtime="sandbox", id=sandbox_id)
    gone = all(not (sandboxes.root / i).exists() for i in ids)
    return ActionResult(gone, output={"destroyed": ids}, summary=f"destroyed {len(ids)} sandbox(es)", verified=gone)
