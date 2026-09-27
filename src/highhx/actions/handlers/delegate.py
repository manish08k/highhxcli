"""Actions backed by an existing HighhX command: the same code path as typing it."""

from __future__ import annotations

from collections.abc import Callable

from highhx.actions.invoke import invoke_cli
from highhx.actions.spec import ActionContext, ActionResult, Handler, Inputs

Argv = Callable[[Inputs], list[str]]


def command_line(argv: list[str]) -> str:
    import shlex

    return "highhx " + " ".join(shlex.quote(a) for a in argv)


def delegate(argv: Argv, *, ok_codes: tuple[int, ...] = (0,)) -> Handler:
    """A handler that runs ``highhx <argv(inputs)>`` in-process and reports its exit code."""

    def handler(ctx: ActionContext, inputs: Inputs) -> ActionResult:
        args = argv(inputs)
        code = invoke_cli(ctx.app, args)
        ok = code in ok_codes
        return ActionResult(
            ok,
            output={"command": command_line(args), "exit_code": code},
            summary=f"{command_line(args)} " + ("succeeded" if ok else f"failed (exit code {code})"),
            error="" if ok else f"exit code {code}",
            status="ok" if ok else ("cancelled" if code == 130 else "failed"),
        )

    return handler


def opt(inputs: Inputs, key: str, flag: str) -> list[str]:
    value = inputs.get(key)
    return [flag, str(value)] if value not in (None, "", False) else []


def flag(inputs: Inputs, key: str, name: str) -> list[str]:
    return [name] if inputs.get(key) else []


def listed(inputs: Inputs, key: str) -> list[str]:
    value = inputs.get(key)
    if value in (None, ""):
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]
