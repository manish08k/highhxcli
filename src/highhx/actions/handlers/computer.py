"""Deterministic computer, browser and file primitives for plain-language automation.

Everything runs through the engine (``Engine.run``) or the computer runtime, after the
executor has classified and approved the action. Keyboard input is never sent to a terminal
emulator — shell commands go through ``!command`` / ``shell.run``, where they are classified
by what they do.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

from highhx.actions.handlers.files import _confine
from highhx.actions.handlers.native import _flow_step, _runtime
from highhx.actions.spec import ActionContext, ActionResult, Inputs
from highhx.agent.permissions import relative_to_root
from highhx.agent.tools.base import ToolError
from highhx.agent.tools.files import FileChange
from highhx.automation.engine.bridge import AutomationBridge, EngineError
from highhx.automation.engine.protocol import MODIFIERS, ProtocolError
from highhx.core.errors import IntegrationError
from highhx.execution.command import CommandSpec
from highhx.language.targets import DEFAULT_SEARCH, Site, default_registry, user_targets_file

TEXT_SUFFIXES = frozenset(
    [
        ".py",
        ".pyi",
        ".md",
        ".txt",
        ".rst",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".css",
        ".scss",
        ".html",
        ".htm",
        ".xml",
        ".csv",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".php",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".swift",
        ".sql",
        ".env.example",
        ".lock",
        ".log",
    ]
)
EXECUTABLE_SUFFIXES = frozenset(
    [
        ".command",
        ".tool",
        ".app",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".exe",
        ".bat",
        ".cmd",
        ".ps1",
        ".msi",
        ".jar",
        ".pkg",
        ".dmg",
        ".scpt",
        ".applescript",
        ".workflow",
        ".run",
        ".bin",
        ".appimage",
        ".deb",
        ".rpm",
    ]
)


# ------------------------------------------------------------------ helpers
def _run(ctx: ActionContext, argv: list[str], *, action: str, policy: str, capture: bool = False) -> str:
    """Run a fixed argv through the engine (already approved by the executor)."""
    result = ctx.app.engine.run(
        CommandSpec(argv, name=argv[0], timeout=60),
        action=action,
        approved=True,
        echo=False,
        record=False,
        policy_action=policy,
        cancel=ctx.cancel,
    )
    if not result.ok and not result.dry_run:
        detail = (result.stderr or result.error or f"exit code {result.exit_code}").strip()
        raise IntegrationError(f"{action} failed: {detail[-300:]}")
    return result.stdout if capture else ""


def bridge(ctx: ActionContext) -> AutomationBridge:
    """The automation bridge for desktop UI operations (see :mod:`highhx.automation.engine`)."""
    return ctx.executor.automation(ctx.cancel)


def _call(ctx: ActionContext, op: str, **args: Any) -> dict[str, Any]:
    """One bridge operation; a refusal (a terminal is frontmost) is the action's error, not a crash."""
    try:
        return bridge(ctx).call(op, **args)
    except ProtocolError as exc:
        raise ToolError(str(exc)) from None
    except EngineError as exc:
        if exc.code == "refused":
            raise ToolError(exc.message) from None
        raise


def frontmost_app(ctx: ActionContext) -> str:
    return str(_call(ctx, "frontmost").get("app") or "")


# ---------------------------------------------------------------- browser
def _app_for(name: str | None) -> Any:
    if not name:
        return None
    registry = default_registry(user_file=user_targets_file())
    app = registry.app(name)
    if app is None:
        raise ToolError(f"I don't know an application called {name!r}.")
    return app


def _open_url_with(ctx: ActionContext, url: str, app: Any) -> ActionResult:
    """Open ``url`` in a browser HighhX cannot drive (Safari, Firefox): the OS opens it there.
    HighhX cannot see that page, so the result is honest about it: opened, not verified."""
    _call(ctx, "open_url", url=url, app=app.platform_name)
    return ActionResult(
        True,
        output={"url": url, "app": app.name},
        summary=f"opened {url} in {app.name} (HighhX can't read {app.name} pages, so it is not verified)",
        verified=None,
    )


def _signin_problem(site: Site | None, url: str) -> str:
    if site is not None and url and site.wants_signin(url):
        return (
            f"{site.name} asks you to sign in. HighhX never signs in for you: sign in once in the HighhX browser "
            "window (it keeps its own profile), then run the request again."
        )
    return ""


def browser_open(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    url = str(inputs["url"])
    app = _app_for(inputs.get("app"))
    if app is not None and not app.automatable:
        return _open_url_with(ctx, url, app)
    result = _flow_step(ctx, {"open": url})
    site = default_registry(user_file=user_targets_file()).site_for_url(url)
    landed = str(result.output.get("url") or "")
    problem = _signin_problem(site, landed)
    if problem:
        return ActionResult(
            False,
            output=result.output,
            summary=f"{site.name if site else url}: sign-in needed",
            error=problem,
            verified=False,
            retryable=False,
        )
    return result


def browser_search(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Open the site's search results for the query and verify that results are showing."""
    query = str(inputs["query"]).strip()
    registry = default_registry(user_file=user_targets_file())
    site = registry.site(str(inputs["site"])) if inputs.get("site") else None
    if inputs.get("site") and (site is None or site.search is None):
        raise ToolError(f"I don't know how to search {inputs['site']!r}.")
    from urllib.parse import quote_plus

    url = site.search_url(query) if site else DEFAULT_SEARCH.format(query=quote_plus(query))
    where = site.name if site else "the web"
    app = _app_for(inputs.get("app"))
    if app is not None and not app.automatable:
        result = _open_url_with(ctx, url, app)
        result.output["query"] = query
        result.summary = f"searched {where} for {query!r} in {app.name} (not verified: HighhX can't read {app.name})"
        return result
    result = _flow_step(ctx, {"open": url})
    landed = str(result.output.get("url") or "")
    result.output["query"] = query
    problem = _signin_problem(site, landed)
    if problem:
        return ActionResult(
            False,
            output=result.output,
            summary=f"{where}: sign-in needed",
            error=problem,
            verified=False,
            retryable=False,
        )
    if not result.ok:
        return result
    marker = site.results if site is not None and site.results else ("/search?q=" if site is None else "")
    showing = not marker or marker in landed
    result.verified = showing
    result.summary = f"searched {where} for {query!r}" + ("" if showing else " — the results page did not load")
    if not showing:
        result.ok, result.status = False, "failed"
        result.error = f"{where} did not show search results (the browser is at {landed or 'no page'})"
    return result


_PLAY_JS = """
(async () => {
  // time-boxed: play() can stay pending (ads, buffering) — never hang the browser connection
  const wait = ms => new Promise(r => setTimeout(r, ms));
  let media = null;
  for (let i = 0; i < 20 && !media; i++) { media = document.querySelector('video, audio'); if (!media) await wait(250); }
  if (!media) return {found: false};
  let error = '';
  await Promise.race([media.play().catch(e => { error = String(e); }), wait(4000)]);
  for (let i = 0; i < 12 && media.paused; i++) await wait(250);
  return {found: true, paused: media.paused, time: media.currentTime, title: document.title, error: error};
})()
"""


def browser_play(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Play the first matching result: results page → first result link → start its media."""
    query = str(inputs["query"]).strip()
    registry = default_registry(user_file=user_targets_file())
    site = registry.site(str(inputs.get("site") or "youtube"))
    if site is None or site.play is None:
        raise ToolError(f"I don't know how to play media on {inputs.get('site')!r} yet.")
    runtime = _runtime(ctx)
    from urllib.parse import quote_plus

    results_url = site.play.results.format(query=quote_plus(query))
    current = runtime.observation.url if runtime.observation is not None else ""
    if current != results_url:
        opened = runtime.navigate(results_url)
        if not opened.ok:
            return ActionResult(False, error="; ".join(opened.problems), summary="could not open the results")
    href = ""
    for _ in range(10):  # results render after load; re-observe until the first result is there
        observation = runtime.observe()
        links = [
            e
            for e in observation.elements
            if e.role == "link" and site.play.result_href in e.attributes.get("href", "")
        ]
        if links:
            chosen = links[0]
            href = chosen.attributes["href"]
            break
        if ctx.cancel.wait(0.5):
            break
    if not href:
        return ActionResult(False, error=f"no {site.name} result for {query!r}", summary="no result found")
    target = href if href.startswith("http") else site.url.rstrip("/") + href
    opened = runtime.navigate(target)
    if not opened.ok:
        return ActionResult(False, error="; ".join(opened.problems), summary="could not open the result")
    state = ctx.computer().browser.evaluate(_PLAY_JS, cancel=ctx.cancel) or {}
    playing = bool(state.get("found")) and not state.get("paused", True)
    title = str(state.get("title") or (runtime.observation.title if runtime.observation else "") or chosen.name)
    return ActionResult(
        playing,
        output={"query": query, "site": site.name, "url": target, "title": title, "playing": playing},
        summary=f"playing {title!r}" if playing else f"opened {title!r} but it is not playing",
        verified=playing and site.play.watch_url in target,
        error="" if playing else str(state.get("error") or "the media did not start"),
    )


def browser_find(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    text = str(inputs["text"]).lower()
    observation = _runtime(ctx).observe()
    found = [
        {"role": e.role, "name": e.name, "target": f"{e.role}:{e.name}"}
        for e in observation.elements
        if text in (e.name or "").lower()
    ][:50]
    return ActionResult(
        True, output={"url": observation.url, "matches": found}, summary=f"{len(found)} match(es) for {text!r}"
    )


# --------------------------------------------------------------- computer
def computer_focus(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Bring an application to the front — launching it first when it is not running — and
    verify that it is frontmost."""
    app = _app_for(str(inputs["app"]))
    if app is None:
        raise ToolError("no application given")
    name = app.platform_name
    launched = False
    if not _call(ctx, "running", app=name).get("running"):
        launched = bool(_call(ctx, "launch", app=name).get("running"))
        if not launched:
            return ActionResult(False, output={"app": app.name}, error=f"{app.name} did not start", verified=False)
    focused = _call(ctx, "focus", app=name)
    ok = bool(focused.get("frontmost"))
    session = ctx.computer()
    session._desktop_app = name
    session._runtimes.pop("desktop", None)
    actual = str(focused.get("actual") or "")
    return ActionResult(
        ok,
        output={"app": app.name, "frontmost": ok, "launched": launched, "engine": bridge(ctx).name},
        summary=f"{'opened and ' if launched else ''}switched to {app.name}",
        verified=ok,
        error="" if ok else f"{app.name} is not in front ({actual or 'another application'} is)",
    )


def computer_type(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    text = str(inputs["text"])
    app = frontmost_app(ctx)
    _call(ctx, "type", text=text)
    return ActionResult(
        True, output={"app": app, "characters": len(text)}, summary=f"typed {len(text)} character(s) into {app}"
    )


def parse_keys(combo: str) -> tuple[list[str], str]:
    parts = [p.strip().lower() for p in combo.replace(" ", "+").split("+") if p.strip()]
    if not parts:
        raise ToolError("no key given")
    modifiers, key = parts[:-1], parts[-1]
    unknown = [m for m in modifiers if m not in MODIFIERS]
    if unknown:
        raise ToolError(f"unknown modifier(s): {', '.join(unknown)} (use cmd, ctrl, alt/option, shift)")
    return modifiers, key


def computer_press(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    key = str(inputs["key"]).lower()
    app = frontmost_app(ctx)
    _call(ctx, "key", key=key)
    return ActionResult(True, output={"app": app, "key": key}, summary=f"pressed {key} in {app}")


def computer_hotkey(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    modifiers, key = parse_keys(str(inputs["keys"]))
    if not modifiers:
        return computer_press(ctx, {"key": key})
    app = frontmost_app(ctx)
    _call(ctx, "hotkey", modifiers=modifiers, key=key)
    combo = "+".join([*modifiers, key])
    return ActionResult(True, output={"app": app, "keys": combo}, summary=f"pressed {combo} in {app}")


def computer_click(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _desktop_step(ctx, {"click": str(inputs["target"])}, float(inputs.get("timeout") or 10))


def computer_scroll(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    direction = str(inputs.get("direction") or "down")
    if str(inputs.get("source") or "browser") == "browser":
        return _flow_step(ctx, {"scroll": direction})
    app = frontmost_app(ctx)
    _call(ctx, "scroll", direction=direction, amount=int(inputs.get("amount") or 3))
    return ActionResult(True, output={"app": app, "direction": direction}, summary=f"scrolled {direction} in {app}")


def _desktop_step(ctx: ActionContext, step: dict[str, Any], timeout: float = 10.0) -> ActionResult:
    """One step through the computer runtime over the bridge: element resolution, the per-element
    safety check, the action, and re-observation to verify it."""
    from highhx.automation.engine.provider import BridgeDesktopProvider
    from highhx.computer.flows import Flow, FlowRunner
    from highhx.computer.runtime import ComputerRuntime

    session = ctx.computer()
    provider = BridgeDesktopProvider(bridge(ctx), session._desktop_app)
    runtime = ComputerRuntime(provider, session.gate, actor=session.actor, tool=session.tool, cancel=ctx.cancel)
    result = FlowRunner(runtime, launch=session.launch).run(Flow("action", [step], timeout))
    entry: dict[str, Any] = result.steps[0] if result.steps else {"ok": False, "error": "nothing ran"}
    detail = str(entry.get("error") or "; ".join(str(p) for p in entry.get("problems") or []))
    verified = entry.get("verified")
    return ActionResult(
        bool(result.ok),
        output={"step": entry},
        summary=str(entry.get("action", "")),
        verified=verified if isinstance(verified, bool) else None,
        error="" if result.ok else detail,
    )


# ------------------------------------------------------------ filesystem
def filesystem_list(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    path = _confine(ctx, str(inputs.get("path") or "."), must_exist=True)
    if not path.is_dir():
        raise ToolError(f"{relative_to_root(ctx.app.root, path)} is not a folder")
    entries = sorted(
        (
            {"name": p.name + ("/" if p.is_dir() else ""), "size": p.stat().st_size if p.is_file() else None}
            for p in path.iterdir()
            if p.name not in (".git",)
        ),
        key=lambda e: (not str(e["name"]).endswith("/"), str(e["name"]).lower()),
    )
    rel = relative_to_root(ctx.app.root, path)
    return ActionResult(True, output={"path": rel, "entries": entries}, summary=f"{rel}: {len(entries)} item(s)")


def filesystem_open(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Open a project file or folder with an application — never *run* it."""
    path = _confine(ctx, str(inputs["path"]), must_exist=True)
    rel = relative_to_root(ctx.app.root, path)
    suffix = "".join(path.suffixes[-1:]).lower()
    app = _app_for(inputs.get("app"))
    if path.is_file() and suffix in EXECUTABLE_SUFFIXES:
        raise ToolError(f"{rel} is executable; opening it could run it. Use `run {rel}` or !command instead.")
    if sys.platform == "darwin":
        if app is not None:
            argv = ["open", "-a", app.platform_name, str(path)]
        elif path.is_file() and (suffix in TEXT_SUFFIXES or not suffix):
            argv = ["open", "-t", str(path)]  # a text editor — never an interpreter
        else:
            argv = ["open", str(path)]
    elif sys.platform.startswith("win"):
        argv = (
            ["notepad", str(path)]
            if path.is_file() and suffix in TEXT_SUFFIXES and app is None
            else ["explorer", str(path)]
        )
    else:
        opener = shutil.which("xdg-open")
        if opener is None:
            raise IntegrationError("xdg-open is not installed.")
        argv = [app.platform_name, str(path)] if app is not None else [opener, str(path)]
    _run(ctx, argv, action=f"Open {rel}", policy=f"exec:{argv[0]}")
    where = app.name if app is not None else ("the file manager" if path.is_dir() else "its default editor")
    return ActionResult(True, output={"path": rel, "with": where}, summary=f"opened {rel} in {where}")


def filesystem_create(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Create a new file or folder — never overwrite (undo removes it)."""
    path = _confine(ctx, str(inputs["path"]), write=True)
    rel = relative_to_root(ctx.app.root, path)
    if path.exists():
        raise ToolError(f"{rel} already exists")
    kind = str(inputs.get("kind") or ("folder" if str(inputs["path"]).endswith("/") else "file"))
    if kind == "folder":
        path.mkdir(parents=True)
    else:
        content = str(inputs.get("content") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        ctx.journal.record(FileChange(path, None, content))
        path.write_text(content, encoding="utf-8")
    return ActionResult(
        True, output={"path": rel, "kind": kind}, summary=f"created {kind} {rel}", changed=[rel], verified=path.exists()
    )


def undo_create(ctx: ActionContext, inputs: Inputs, result: ActionResult) -> str:
    path = _confine(ctx, str(result.output["path"]), write=True)
    if result.output.get("kind") == "folder":
        try:
            path.rmdir()
        except OSError:
            return f"compensation failed: {result.output['path']} is not empty"
        return f"removed {result.output['path']}/"
    path.unlink(missing_ok=True)
    return f"removed {result.output['path']}"


def verify_exists(ctx: ActionContext, inputs: Inputs, result: ActionResult) -> tuple[bool, str]:
    return (ctx.app.root / str(result.output["path"])).exists(), "it does not exist after creating it"


def project_path(root: Path, raw: str) -> Path | None:
    """``raw`` as an existing path inside ``root`` (for the resolver), else None."""
    candidate = raw.strip().strip("\"'`")
    try:
        path = (root / candidate).resolve()
    except OSError:
        return None
    base = root.resolve()
    if (path == base or path.is_relative_to(base)) and path.exists():
        return path
    return None
