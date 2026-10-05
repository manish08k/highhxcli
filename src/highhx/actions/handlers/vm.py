"""``vm.*`` handlers: virtual machines through Lima or Tart (see highhx.runtimes.vm)."""

from __future__ import annotations

import shlex
from typing import Any

from highhx.actions.spec import ActionContext, ActionResult, Inputs
from highhx.agent.tools.base import ToolError
from highhx.drivers.base import CapabilityError
from highhx.runtimes import vm as vms


def _backend(inputs: Inputs) -> Any:
    wanted = str(inputs.get("backend") or "")
    if wanted:
        cls = vms.BACKENDS.get(wanted)
        if cls is None:
            raise ToolError(f"Unknown VM backend {wanted!r} (lima or tart).")
        if not cls.available():
            raise ToolError(f"{cls.binary} is not installed.")
    else:
        cls = next((c for c in vms.BACKENDS.values() if c.available()), None)
        if cls is None:
            raise ToolError("No virtual-machine backend is installed: install Lima (brew install lima) or Tart.")
    return cls(runner=vms.DEFAULT_RUNNER, spawn=vms.DEFAULT_SPAWN)


def _guard(fn: Any) -> Any:
    def run(ctx: ActionContext, inputs: Inputs) -> ActionResult:
        try:
            return fn(ctx, inputs)
        except CapabilityError as exc:
            return ActionResult(False, error=exc.message + (f" ({exc.hint})" if exc.hint else ""))

    run.__name__ = fn.__name__
    return run


@_guard
def vm_list(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    try:
        backend = _backend(inputs)
    except ToolError as exc:
        return ActionResult(True, output={"available": False, "detail": str(exc), "vms": []}, summary=str(exc))
    found = [v.to_dict() for v in backend.list()]
    return ActionResult(
        True,
        output={"available": True, "backend": backend.name, "vms": found},
        summary=f"{len(found)} VM(s) ({backend.name})",
    )


@_guard
def vm_create(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    backend = _backend(inputs)
    limits = vms.VMLimits(
        int(inputs.get("cpus") or 2), int(inputs.get("memory_mb") or 4096), int(inputs.get("disk_gb") or 30)
    )
    name = vms.check_name(str(inputs["name"]))
    backend.create(name, str(inputs.get("image") or ""), limits)
    return ActionResult(
        True,
        output={"name": name, "backend": backend.name, **limits.__dict__},
        summary=f"created VM {name} ({backend.name})",
        verified=any(v.name == name for v in backend.list()),
    )


def _lifecycle(op: str) -> Any:
    @_guard
    def handler(ctx: ActionContext, inputs: Inputs) -> ActionResult:
        backend = _backend(inputs)
        name = vms.check_name(str(inputs["name"]))
        if op in ("snapshot", "restore"):
            getattr(backend, op)(name, str(inputs["tag"]))
        else:
            getattr(backend, op)(name)
        state = next((v.status for v in backend.list() if v.name == name), "missing")
        expected = {
            "start": "running",
            "resume": "running",
            "stop": "stopped",
            "pause": "suspended",
            "destroy": "missing",
        }.get(op)
        ok = (
            expected is None or state == expected or (op in ("start", "resume") and backend.name == "tart")
        )  # tart run is asynchronous
        return ActionResult(
            ok,
            output={"name": name, "state": state},
            summary=f"{op} {name}: {state}",
            verified=expected is not None and state == expected,
            error="" if ok else f"VM {name} is {state}, not {expected}",
        )

    handler.__name__ = f"vm_{op}"
    return handler


vm_start, vm_stop, vm_pause, vm_resume, vm_destroy = (
    _lifecycle(op) for op in ("start", "stop", "pause", "resume", "destroy")
)
vm_snapshot, vm_restore = _lifecycle("snapshot"), _lifecycle("restore")


def command_of(inputs: Inputs) -> str:
    return str(inputs.get("command") or shlex.join([str(a) for a in inputs.get("argv") or []]))


@_guard
def vm_exec(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    runtime = vms.VMRuntime(str(inputs["name"]), backend=_backend(inputs))
    argv = [str(a) for a in inputs.get("argv") or []] or ["sh", "-c", str(inputs.get("command") or "")]
    result = runtime.exec(argv, timeout=float(inputs.get("timeout") or 600))
    return ActionResult(
        result.ok,
        output=result.to_dict(),
        summary=f"exit code {result.exit_code} in VM {runtime.name}",
        error="" if result.ok else (result.stderr.strip()[:300] or f"exit code {result.exit_code}"),
    )


def remote_check(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    from highhx.runtimes.remote import RemoteRuntime

    report = RemoteRuntime(str(inputs["target"]), cwd=ctx.app.root).heartbeat()
    summary = f"{report['target']}: " + (
        f"answering in {report['latency_ms']} ms" + (f", HighhX {report['highhx']}" if report.get("highhx") else "")
        if report["reachable"]
        else "not reachable"
    )
    return ActionResult(
        bool(report["reachable"]),
        output=report,
        summary=summary,
        verified=bool(report["reachable"]),
        error="" if report["reachable"] else str(report.get("error") or ""),
    )
