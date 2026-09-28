"""Actions implemented directly on HighhX services (git through the engine, browser through
the computer runtime, project detection)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from highhx.actions.handlers.delegate import command_line
from highhx.actions.invoke import invoke_cli
from highhx.actions.spec import ActionContext, ActionResult, Inputs
from highhx.agent.permissions import confine_path, relative_to_root
from highhx.agent.tools.base import ToolError
from highhx.execution.command import CommandSpec
from highhx.safety.actions import Actor


# -------------------------------------------------------------------- project
def project_detect(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    from highhx.actions.context import build_context

    context = build_context(ctx.app)
    return ActionResult(True, output=context, summary=f"{context['name']}: {context['stack'] or 'unknown stack'}")


# ------------------------------------------------------------------------ git
def _git(ctx: ActionContext, argv: list[str], *, policy: str, action: str) -> tuple[bool, str, int]:
    result = ctx.app.engine.run(
        CommandSpec(["git", *argv], cwd=ctx.app.root, name="git"),
        action=action,
        policy_action=policy,
        cancel=ctx.cancel,
    )
    text = "\n".join(x for x in (result.stdout, result.stderr) if x).strip()
    return result.ok or result.dry_run, text, result.exit_code if result.exit_code is not None else 1


def _head(ctx: ActionContext) -> str | None:
    try:
        return ctx.app.git_repo.head()
    except Exception:
        return None


def _branch(ctx: ActionContext) -> str | None:
    try:
        return ctx.app.git_repo.current_branch()
    except Exception:
        return None


def _outcome(ok: bool, text: str, code: int, summary: str, **output: Any) -> ActionResult:
    return ActionResult(
        ok,
        output={"exit_code": code, "output": text[-4000:], **output},
        summary=summary if ok else f"{summary} failed (exit code {code})",
        error="" if ok else (text.splitlines()[-1] if text else f"exit code {code}"),
    )


def git_push(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    remote = str(inputs.get("remote") or "origin")
    argv = ["push", remote] + ([str(inputs["branch"])] if inputs.get("branch") else [])
    if inputs.get("tags"):
        argv.append("--tags")
    ok, text, code = _git(ctx, argv, policy="git:push", action=f"Push to {remote}")
    return _outcome(ok, text, code, f"pushed to {remote}", remote=remote)


def git_pull(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    remote = str(inputs.get("remote") or "origin")
    before = _head(ctx)
    ok, text, code = _git(ctx, ["pull", "--ff-only", remote], policy="git:pull", action=f"Pull from {remote}")
    return _outcome(ok, text, code, f"pulled from {remote}", before=before, after=_head(ctx))


def undo_pull(ctx: ActionContext, inputs: Inputs, result: ActionResult) -> str:
    before = result.output.get("before")
    if not before or before == result.output.get("after"):
        return "nothing to undo"
    ok, text, _ = _git(ctx, ["reset", "--keep", str(before)], policy="git:reset", action="Undo the pull")
    return f"reset to {before[:10]}" if ok else f"compensation failed: {text}"


def git_checkout(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    ref = str(inputs["ref"])
    previous = _branch(ctx) or _head(ctx)
    argv = ["switch", "-c", ref] if inputs.get("create") else ["switch", ref]
    ok, text, code = _git(ctx, argv, policy="git:checkout", action=f"Switch to {ref}")
    return _outcome(ok, text, code, f"switched to {ref}", ref=ref, previous=previous)


def undo_checkout(ctx: ActionContext, inputs: Inputs, result: ActionResult) -> str:
    previous = result.output.get("previous")
    if not previous:
        return "compensation failed: previous branch unknown"
    ok, text, _ = _git(ctx, ["switch", str(previous)], policy="git:checkout", action=f"Switch back to {previous}")
    return f"switched back to {previous}" if ok else f"compensation failed: {text}"


def git_revert(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    ref = str(inputs.get("ref") or "HEAD")
    ok, text, code = _git(ctx, ["revert", "--no-edit", ref], policy="git:revert", action=f"Revert {ref}")
    return _outcome(ok, text, code, f"reverted {ref}", head=_head(ctx))


def git_commit(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    argv = ["git", "commit", "-m", str(inputs["message"])]
    if inputs.get("all"):
        argv.append("--all")
    argv += [str(p) for p in inputs.get("paths") or []]
    before = _head(ctx)
    code = invoke_cli(ctx.app, argv)
    after = _head(ctx)
    ok = code == 0 and after is not None and after != before
    return ActionResult(
        ok,
        output={"command": command_line(argv), "exit_code": code, "commit": after, "parent": before},
        summary=f"committed {after[:10]}" if ok and after else f"commit failed (exit code {code})",
        error="" if ok else f"exit code {code}",
    )


def undo_commit(ctx: ActionContext, inputs: Inputs, result: ActionResult) -> str:
    commit = result.output.get("commit")
    if not commit or _head(ctx) != commit:
        return "compensation failed: HEAD moved since the commit"
    ok, text, _ = _git(ctx, ["reset", "--soft", "HEAD~1"], policy="git:reset", action="Undo the commit (keep changes)")
    return f"undid commit {commit[:10]} (changes kept)" if ok else f"compensation failed: {text}"


def undo_tag(ctx: ActionContext, inputs: Inputs, result: ActionResult) -> str:
    name = str(inputs["name"])
    ok, text, _ = _git(ctx, ["tag", "-d", name], policy="git:tag", action=f"Delete tag {name}")
    return f"deleted tag {name}" if ok else f"compensation failed: {text}"


# -------------------------------------------------------------------- browser
def _runtime(ctx: ActionContext) -> Any:
    session = ctx.computer()
    session.cancel = ctx.cancel
    runtime = session.runtime("browser")
    runtime.cancel = ctx.cancel
    return runtime


def _flow_step(ctx: ActionContext, step: dict[str, Any], timeout: float = 10.0) -> ActionResult:
    """One deterministic step through the flow runner (element resolution, per-element safety
    check, execution, re-observation and verification)."""
    from highhx.computer.flows import Flow, FlowRunner

    runtime = _runtime(ctx)
    result = FlowRunner(runtime, launch=ctx.computer().launch).run(Flow("action", [step], timeout))
    entry: dict[str, Any] = result.steps[0] if result.steps else {"ok": False, "error": "nothing ran"}
    observation = runtime.observation
    output: dict[str, Any] = {"step": entry}
    if observation is not None:
        output["url"] = observation.url
        output["title"] = observation.title
    problems = entry.get("problems") or []
    detail = str(entry.get("error") or "; ".join(str(p) for p in problems))
    verified = entry.get("verified")
    return ActionResult(
        bool(result.ok),
        output=output,
        summary=str(entry.get("action", "")) + ("" if result.ok else f" — {detail}"),
        verified=verified if isinstance(verified, bool) else None,
        error="" if result.ok else detail,
    )


def open_url(ctx: ActionContext, url: str, *, reuse_tab: bool = False) -> ActionResult:
    """Open ``url`` in the HighhX browser (with ``reuse_tab``: switch to a tab already showing
    it) and report where the browser really is — verified by the runtime."""
    from highhx.core.errors import NotFoundError, UsageError, ValidationError

    runtime = _runtime(ctx)
    label = f"open {url}"
    try:
        outcome = runtime.navigate(url, reuse_tab=reuse_tab)
    except (NotFoundError, UsageError, ValidationError) as exc:
        return ActionResult(
            False, output={"step": {"action": label, "ok": False, "error": exc.message}}, error=exc.message
        )
    observation = outcome.observation
    detail = "; ".join(outcome.problems)
    entry: dict[str, Any] = {"action": label, "ok": outcome.ok, "verified": outcome.verified}
    if outcome.problems:
        entry["problems"] = outcome.problems
    output: dict[str, Any] = {"step": entry}
    if observation is not None:
        output["url"], output["title"] = observation.url, observation.title
    return ActionResult(
        outcome.ok,
        output=output,
        summary=outcome.summary[:1].lower() + outcome.summary[1:] + ("" if outcome.ok else f" — {detail}"),
        verified=outcome.verified if isinstance(outcome.verified, bool) else None,
        error="" if outcome.ok else detail,
    )


def browser_open(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return open_url(ctx, str(inputs["url"]))


def browser_click(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _flow_step(ctx, {"click": str(inputs["target"])}, float(inputs.get("timeout") or 10))


def browser_fill(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    value: dict[str, Any] = {"into": str(inputs["target"])}
    if inputs.get("text_from_env"):
        value["text_from_env"] = str(inputs["text_from_env"])
    else:
        value["text"] = str(inputs.get("text") or "")
    return _flow_step(ctx, {"type": value}, float(inputs.get("timeout") or 10))


def browser_press(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _flow_step(ctx, {"press": str(inputs["key"])})


def browser_wait(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    expect = {k: inputs[k] for k in ("text", "url_contains", "title_contains", "element") if inputs.get(k)}
    if expect:
        return _flow_step(ctx, {"expect": expect}, float(inputs.get("timeout") or 10))
    seconds = float(inputs.get("seconds") or 1)
    ctx.cancel.wait(seconds)
    return ActionResult(True, output={"waited": seconds}, summary=f"waited {seconds:g}s")


def browser_extract(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    runtime = _runtime(ctx)
    if inputs.get("url"):
        outcome = runtime.navigate(str(inputs["url"]))
        if not outcome.ok:
            return ActionResult(False, error="; ".join(outcome.problems), summary="could not open the page")
    observation = runtime.observe()
    roles = set(inputs.get("roles") or [])
    elements = [
        {"role": e.role, "name": e.name, **({"value": e.value} if getattr(e, "value", None) else {})}
        for e in observation.elements
        if not roles or e.role in roles
    ][: int(inputs.get("limit") or 200)]
    return ActionResult(
        True,
        output={
            "url": observation.url,
            "title": observation.title,
            "text": (observation.text or "")[: int(inputs.get("max_text") or 20000)],
            "elements": elements,
        },
        summary=f"{observation.title or observation.url} · {len(elements)} element(s)",
    )


def browser_screenshot(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    runtime = _runtime(ctx)
    if runtime.observation is None:
        runtime.observe()
    raw = ctx.computer().browser.screenshot(cancel=ctx.cancel)
    if not raw:
        raise ToolError("the browser returned no screenshot")
    default = f".highhx/screenshots/{time.strftime('%Y%m%d-%H%M%S')}.png"
    raw_path = str(inputs.get("path") or default)
    target = (
        ctx.app.root / raw_path
        if raw_path.startswith(".highhx/screenshots/")
        else confine_path(ctx.app, raw_path, write=True, for_agent=ctx.actor == Actor.AGENT)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_bytes(raw)
    rel = relative_to_root(ctx.app.root, target)
    return ActionResult(True, output={"path": rel, "bytes": len(raw)}, summary=f"screenshot saved to {rel}")


def app_launch(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _flow_step(ctx, {"launch": str(inputs["name"])})


# ------------------------------------------------------------- tabs & history
def _page_step(ctx: ActionContext, op: str, value: str | None = None) -> ActionResult:
    result = _flow_step(ctx, {op: value if value else True})
    result.output["tab"] = ctx.computer().browser._target_id
    return result


def browser_back(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _page_step(ctx, "back")


def browser_forward(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _page_step(ctx, "forward")


def browser_refresh(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _page_step(ctx, "refresh")


def browser_new_tab(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _page_step(ctx, "new_tab", str(inputs["url"]) if inputs.get("url") else None)


def browser_close_tab(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _page_step(ctx, "close_tab")


def browser_switch_tab(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _page_step(ctx, "switch_tab", str(inputs["tab"]))


def browser_tabs(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    tabs = ctx.computer().browser.list_tabs(cancel=ctx.cancel)
    return ActionResult(True, output={"tabs": tabs}, summary=f"{len(tabs)} tab(s) open")


# ------------------------------------------------------------ element actions
def browser_hover(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _flow_step(ctx, {"hover": str(inputs["target"])}, float(inputs.get("timeout") or 10))


def browser_double_click(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _flow_step(ctx, {"double_click": str(inputs["target"])}, float(inputs.get("timeout") or 10))


def browser_drag(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    step = {"drag": {"from": str(inputs["source"]), "to": str(inputs["target"])}}
    return _flow_step(ctx, step, float(inputs.get("timeout") or 10))


def browser_upload(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Choose project files in a file field — never secret files, never outside the project
    (uploading is data leaving the machine, so the gate asks first)."""
    files = [
        str(confine_path(ctx.app, str(raw), must_exist=True, for_agent=ctx.actor == Actor.AGENT))
        for raw in inputs["paths"]
    ]
    return _flow_step(ctx, {"upload": {"into": str(inputs["target"]), "files": files}})


def browser_download(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    result = _flow_step(ctx, {"download": str(inputs["target"])}, float(inputs.get("timeout") or 10))
    download = _runtime(ctx).last_download
    if download:
        result.output["download"] = download
        if download.get("path"):
            result.summary = f"downloaded {download['path']}"
    return result
