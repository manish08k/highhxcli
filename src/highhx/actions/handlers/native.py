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


class network_evidence:
    """The requests the page made while an action ran, added to the action's result
    (``output["network"]``) and emitted as ``network.observed``. Pages without a network journal
    (simulations) add nothing."""

    def __init__(self, ctx: ActionContext) -> None:
        self.ctx = ctx
        self.browser = getattr(ctx.computer(), "browser", None)  # the object only: nothing is started here
        journal = getattr(self.browser, "network", None)
        self.journal = journal if callable(getattr(journal, "mark", None)) else None
        self.mark = self.journal.mark() if self.journal is not None else None

    def attach(self, result: ActionResult) -> ActionResult:
        if self.journal is None or self.mark is None:
            return result
        pump = getattr(self.browser, "pump_events", None)
        if callable(pump):
            pump(0.25, cancel=self.ctx.cancel)
        entries = self.journal.since(self.mark)
        result.output["network"] = entries
        if entries:
            from highhx.actions import events as ev

            failed = [e for e in entries if e.get("error") or (e.get("status") or 0) >= 400]
            self.ctx.app.ctx.events.emit(
                ev.NETWORK_OBSERVED, requests=len(entries), failed=len(failed), entries=entries[:20]
            )
        return result


def _flow_step(ctx: ActionContext, step: dict[str, Any], timeout: float = 10.0) -> ActionResult:
    evidence = network_evidence(ctx)
    return evidence.attach(_run_flow_step(ctx, step, timeout))


def _run_flow_step(ctx: ActionContext, step: dict[str, Any], timeout: float = 10.0) -> ActionResult:
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
    evidence = network_evidence(ctx)
    return evidence.attach(_open_url(ctx, url, reuse_tab=reuse_tab))


def _open_url(ctx: ActionContext, url: str, *, reuse_tab: bool = False) -> ActionResult:
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


NETWORK_LOOKBACK = 2.0
"""Seconds before a wait began that still count for ``request`` (the click that sent it came first)."""


def _wait_for_network(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """``network_idle``: no request in flight and none started or finished for ``idle_ms``.
    ``request``: a request matching ``url_contains`` / ``method`` / ``status`` finished during
    the wait or in the :data:`NETWORK_LOOKBACK` seconds before it. Evidence comes from the
    sanitized journal only; without one the wait fails rather than guessing."""
    browser = getattr(ctx.computer(), "browser", None)
    journal: Any = getattr(browser, "network", None)
    if browser is None or journal is None or not callable(getattr(journal, "inflight", None)):
        return ActionResult(False, error="network evidence is not available for this browser")
    if getattr(browser, "_conn", None) is None:
        _runtime(ctx).observe()  # connect (the journal fills only while connected)
    timeout = float(inputs.get("timeout") or 30)
    deadline = time.monotonic() + timeout
    began = time.monotonic()
    wanted = dict(inputs.get("request") or {})
    idle = float(inputs.get("idle_ms") or 500) / 1000
    while True:
        browser.pump_events(0.1, cancel=ctx.cancel)
        if wanted:
            status = wanted.get("status")
            found = journal.find(
                0,
                url_contains=str(wanted.get("url_contains") or ""),
                method=str(wanted.get("method") or ""),
                status=int(status) if status is not None else None,
                finished_after=began - NETWORK_LOOKBACK,
            )
            if found is not None:
                return ActionResult(
                    True,
                    output={"network": [found], "waited": round(time.monotonic() - began, 3)},
                    summary=f"{found['method']} {found['url']} → {found.get('status') or found.get('error')}",
                    verified=True,
                )
        elif journal.inflight() == 0 and journal.idle_for() >= idle:
            waited = round(time.monotonic() - began, 3)
            return ActionResult(
                True, output={"waited": waited}, summary=f"network idle after {waited:g}s", verified=True
            )
        if ctx.cancel.cancelled:
            from highhx.core.errors import OperationCancelledError

            raise OperationCancelledError("Wait cancelled.")
        if time.monotonic() > deadline:
            what = (
                f"a request matching {wanted}" if wanted else f"the network to be idle ({journal.inflight()} in flight)"
            )
            return ActionResult(False, error=f"timed out after {timeout:g}s waiting for {what}")


def browser_wait(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    if inputs.get("network_idle") or inputs.get("request"):
        return _wait_for_network(ctx, inputs)
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
    if inputs.get("schema") is not None:
        return _extract_structured(ctx, inputs["schema"])
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


def _extract_structured(ctx: ActionContext, schema: Any) -> ActionResult:
    """The page as data shaped by ``schema`` (see :mod:`highhx.computer.extract`): explicit labels,
    tables and lists only; missing required or ambiguous fields fail instead of being guessed."""
    from highhx.computer.extract import STRUCTURE_JS, check_schema, extract

    problems = check_schema(schema)
    if not problems and schema.get("type") != "object":
        problems = ["schema: the top level must be an object"]
    if problems:
        return ActionResult(False, error="unsupported schema: " + "; ".join(problems[:5]))
    page = ctx.computer().browser.evaluate(STRUCTURE_JS, cancel=ctx.cancel, retry_safe=True) or {}
    found = extract(schema, page)
    detail = "; ".join([*(f"missing {m}" for m in found.missing), *found.problems][:8])
    return ActionResult(
        found.ok,
        output={
            "url": str(page.get("url") or ""),
            "title": str(page.get("title") or ""),
            "data": found.data,
            "sources": found.sources,
            "missing": found.missing,
            "problems": found.problems,
        },
        summary=f"extracted {len(found.data)} of {len(schema['properties'])} field(s)"
        + (f" — {detail}" if detail else ""),
        verified=found.ok,
        error="" if found.ok else detail,
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
    from highhx.artifacts import record

    artifact = record(ctx, target, kind="screenshot", action="browser.screenshot")
    # and a page capture to click from: one pixel per CSS pixel, grounded and refused once stale
    # (a browser that cannot make one still saves its screenshot)
    browser = ctx.computer().browser
    capture = (
        ctx.computer().captures.take_page(browser, cancel=ctx.cancel)
        if callable(getattr(browser, "page_capture", None))
        else None
    )
    shown = {"capture": capture.id, "width": capture.shot.width, "height": capture.shot.height} if capture else {}
    return ActionResult(
        True,
        output={"path": rel, "bytes": len(raw), "artifact": artifact, **shown},
        summary=f"screenshot saved to {rel}" + (f" (capture {capture.id} for browser.click_at)" if capture else ""),
    )


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


def browser_right_click(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _flow_step(ctx, {"right_click": str(inputs["target"])}, float(inputs.get("timeout") or 10))


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
            from highhx.artifacts import record

            result.output["artifact"] = record(ctx, str(download["path"]), kind="download", action="browser.download")
    return result


# ------------------------------------------------------------- browser profiles
def _profiles(ctx: ActionContext) -> Any:
    store = getattr(ctx.computer(), "profiles", None)
    if store is None:
        from highhx.computer.profiles import ProfileStore
        from highhx.utils.paths import user_data_dir

        store = ProfileStore(user_data_dir() / "computer")
    return store


def browser_profiles(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    found = [p.to_dict() for p in _profiles(ctx).list()]
    return ActionResult(True, output={"profiles": found}, summary=f"{len(found)} profile(s)")


def browser_profile_create(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    info = _profiles(ctx).create(str(inputs["name"]))
    return ActionResult(True, output=info.to_dict(), summary=f"created browser profile {info.name}", verified=True)


def browser_profile_delete(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    store = _profiles(ctx)
    name = str(inputs["name"])
    store.delete(name)
    gone = not store.exists(name)
    return ActionResult(gone, output={"name": name}, summary=f"deleted browser profile {name}", verified=gone)


def browser_profile_import(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    info = _profiles(ctx).import_from(str(inputs["name"]), Path(str(inputs["source"])))
    return ActionResult(True, output=info.to_dict(), summary=f"imported browser profile {info.name}", verified=True)


# ------------------------------------------------------------- browser sessions
def _sessions(ctx: ActionContext) -> Any:
    from highhx.computer.browser_sessions import BrowserSessionManager

    store = _profiles(ctx)
    return BrowserSessionManager(store.base, headless=getattr(ctx.computer(), "headless", None))


def browser_sessions(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    manager = _sessions(ctx)
    removed = manager.cleanup() if inputs.get("cleanup") else []
    found = [s.to_dict() for s in manager.list()]
    return ActionResult(True, output={"sessions": found, "removed": removed}, summary=f"{len(found)} session(s)")


def browser_session_start(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    session = _sessions(ctx).start(
        profile=str(inputs.get("profile") or "default"), endpoint=str(inputs.get("endpoint") or ""), cancel=ctx.cancel
    )
    return ActionResult(
        True,
        output=session.to_dict(),
        summary=f"browser session {session.id} ({session.kind}, {session.profile})",
        verified=True,
    )


def browser_session_stop(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    _sessions(ctx).stop(str(inputs["session"]))
    return ActionResult(
        True, output={"session": inputs["session"]}, summary=f"stopped browser session {inputs['session']}"
    )


def browser_session_heartbeat(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    manager = _sessions(ctx)
    session = manager.heartbeat(str(inputs["session"]))
    if not session.healthy and inputs.get("reconnect"):
        session = manager.reconnect(str(inputs["session"]), cancel=ctx.cancel)
    return ActionResult(
        session.healthy,
        output=session.to_dict(),
        summary=f"{session.id}: {'answering' if session.healthy else 'not answering'}",
        verified=session.healthy,
        error="" if session.healthy else "the browser does not answer (reconnect: true restarts a local one)",
    )
