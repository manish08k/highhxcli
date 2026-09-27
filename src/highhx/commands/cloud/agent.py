"""highhx agent — HighhX Pro: the AI developer agent."""

from __future__ import annotations

import sys
from typing import Any

import click

from highhx.agent.model.registry import PROVIDER_NAMES, provider_info
from highhx.agent.settings import EFFORTS
from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.core.errors import AccountError, ExitCode, UsageError
from highhx.safety.gate import ApprovalMode


@click.group("agent", cls=DefaultGroup, default_command="run", short_help="HighhX Pro: the AI developer agent.")
def agent() -> None:
    """HighhX Pro: an AI developer agent in your terminal.

    Describe what you want in plain language — "fix my failing tests",
    "explain how this project works", "make this project production ready".
    The agent inspects the project, proposes a plan, works through HighhX's
    tools (tests, checks, builds, git, security, deploy …) and verifies the
    result. Every risky action goes through HighhX's policies and approvals.
    """


def _read_stdin_prompt() -> str:
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    return sys.stdin.read().strip()


@agent.command("run", short_help="Start the agent (interactive), or run one request.")
@click.argument("prompt", nargs=-1)
@click.option("--continue", "-c", "cont", is_flag=True, help="Continue the most recent session in this project.")
@click.option("--resume", "resume_id", metavar="ID", help="Resume a saved session (see `highhx agent sessions`).")
@click.option(
    "--provider", type=click.Choice(PROVIDER_NAMES), help="AI provider (default: account setting, else highhx)."
)
@click.option("--model", metavar="MODEL", help="Model to use (default: provider default).")
@click.option(
    "--mode",
    type=click.Choice([m.value for m in ApprovalMode]),
    help="Approvals: ask (default), auto-edit (normal changes without asking), read-only.",
)
@click.option("--max-steps", type=click.IntRange(1, 500), help="Maximum tool steps per request.")
@click.option("--effort", type=click.Choice(EFFORTS), help="Reasoning effort, where the model supports it.")
@pass_app
def agent_run(
    app: App,
    prompt: tuple[str, ...],
    cont: bool,
    resume_id: str | None,
    provider: str | None,
    model: str | None,
    mode: str | None,
    max_steps: int | None,
    effort: str | None,
) -> int:
    """In a terminal, start the interactive HighhX session (the same one as bare
    `highhx`; PROMPT becomes the first request). When input/output is not a
    terminal, or with --json, handle PROMPT as one AI agent request and exit.

    \b
      highhx agent
      highhx agent "why is the application crashing?"
      highhx agent --continue
      highhx agent --mode read-only "find security issues"
      echo "run all the tests and fix whatever fails" | highhx agent --yes --mode auto-edit

    Exit code: 0 when the request completed, 1 when it stopped early (step limit,
    output limit, refusal, interruption), 10 without a HighhX Pro account
    (one-shot requests only; the interactive session works on every plan).
    """
    # Imported here so Free commands do not pay for loading the agent at start-up.
    from highhx.agent.bootstrap import Resume, create_session
    from highhx.agent.running import registered
    from highhx.agent.ui import TerminalUI, format_tokens
    from highhx.commands.cloud.account import render_upsell

    out = app.output
    interactive = app.options.is_interactive()
    text = " ".join(prompt).strip()
    overrides: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "approval": mode,
        "max_steps": max_steps,
        "effort": effort,
    }
    from highhx.agent.launch import interactive_terminal, start_interactive

    if getattr(app, "interactive_session", False):
        raise UsageError("You are already in the HighhX session.", hint="Type your request at the prompt.")
    if interactive_terminal(app):
        # The same interactive session as bare `highhx`; the account decides whether the AI agent attaches.
        return start_interactive(app, first=text or None, overrides=overrides, resume=Resume(resume_id, cont))
    # In a terminal the agent is interactive (PROMPT becomes the first message);
    # otherwise — pipes, CI, --json — it handles one request and exits.
    one_shot = app.options.json or not interactive
    if one_shot and not text:
        text = _read_stdin_prompt()
        if not text:
            raise UsageError(
                "No request given.",
                hint='Run `highhx agent` in a terminal, or pass a request: highhx agent "fix my failing tests".',
            )
    console = out.err_console if app.options.json or app.options.quiet else out.console
    ui = TerminalUI(console, out.symbols, interactive=interactive and not app.options.json)
    cloud = app.cloud
    try:
        session, account, resumed = create_session(app, cloud, ui, overrides=overrides, resume=Resume(resume_id, cont))
    except AccountError:
        if out.human:
            account_obj = None
            if cloud.signed_in:
                try:
                    account_obj = cloud.account()
                except AccountError:
                    account_obj = None
            render_upsell(out.err_console, account_obj, signed_in=cloud.signed_in)
            out.err_console.print()
        raise

    session_id = session.record.id if session.record else None
    if not one_shot:
        from highhx.agent.repl import AgentREPL

        with registered(session_id, app.root):
            return AgentREPL(session, ui, cloud, account).run(text or None, resumed=resumed)

    try:
        with registered(session_id, app.root):
            result = session.run_turn(text, cancel=app.ctx.cancel)
    finally:
        session.close()
    completed = result.stopped == "completed"
    if app.options.json:
        out.json(
            {
                "ok": completed,
                "stopped": result.stopped,
                "text": result.text,
                "steps": result.steps,
                "tools": [{"name": name, "ok": ok} for name, ok in result.tools],
                "changed_files": result.changed_files,
                "usage": result.usage.to_dict(),
                "seconds": round(result.seconds, 2),
                "session_id": session.record.id if session.record else None,
            }
        )
    elif not app.options.quiet:
        parts = [f"{result.steps} steps"] if result.steps else []
        if result.changed_files:
            parts.append(f"{len(result.changed_files)} files changed")
        if result.usage.total:
            parts.append(f"{format_tokens(result.usage.total)} tokens")
        parts.append(f"{result.seconds:.0f}s")
        ui.print()
        ui.footer(parts)
    return 0 if completed else int(ExitCode.FAILURE)


@agent.command("sessions", short_help="List saved agent sessions.")
@click.option("--all", "all_projects", is_flag=True, help="Sessions from every project, not just this one.")
@click.option("--limit", type=click.IntRange(1, 500), default=20, show_default=True, help="Maximum sessions to list.")
@pass_app
def agent_sessions(app: App, all_projects: bool, limit: int) -> int:
    """Saved sessions (newest first). Resume one with `highhx agent --resume ID`."""
    from highhx.agent.history import SessionStore
    from highhx.agent.ui import format_tokens

    out = app.output
    if app.db is None:
        out.emit({"sessions": []}, lambda: out.warn("Session storage is unavailable."))
        return 1
    records = SessionStore(app.db, app.redactor).list(root=None if all_projects else str(app.root), limit=limit)
    rows = [r for r in records if r.turns > 0]
    out.emit(
        {"sessions": [r.to_dict() for r in rows]},
        lambda: (
            out.table(
                ["id", "updated", "turns", "tokens", "provider", "title"],
                [
                    (
                        r.id,
                        r.updated_at.replace("T", " ")[:16],
                        r.turns,
                        format_tokens(r.usage.total),
                        r.provider,
                        r.title,
                    )
                    for r in rows
                ],
            )
            if rows
            else out.info("No agent sessions yet. Start one with `highhx agent`.")
        ),
    )
    return 0


@agent.command("models", short_help="AI providers and models the agent can use.")
@pass_app
def agent_models(app: App) -> int:
    """AI providers and models the HighhX gateway can route agent requests to."""
    rows = []
    for name in PROVIDER_NAMES:
        info = provider_info(name)
        rows.append(
            {
                "provider": name,
                "label": info.label,
                "default_model": info.default_model,
                "models": list(info.models),
                "access": "HighhX gateway" + (" (routes by model / account setting)" if name == "highhx" else ""),
            }
        )
    out = app.output
    out.emit(
        {"providers": rows},
        lambda: out.table(
            ["provider", "default", "models", "access"],
            [(r["provider"], r["default_model"], ", ".join(r["models"][:5]), r["access"]) for r in rows],
        ),
    )
    if out.human:
        out.note("All AI requests go through the HighhX platform (authenticated, plan-checked and metered).")
        out.note("Change the default with `highhx account settings --provider … --model …`.")
    return 0


@agent.command("stop", short_help="Stop running agents immediately (kill switch).")
@click.option("--all", "all_agents", is_flag=True, help="Stop agents in every project, not just this one.")
@click.option("--session", "session_id", metavar="ID", help="Stop the agent working on this session.")
@pass_app
def agent_stop(app: App, all_agents: bool, session_id: str | None) -> int:
    """Cancel running `highhx agent` processes: the current model request is cancelled
    (also on the platform), retries stop, running commands and their child processes are
    terminated, and the session is saved as cancelled."""
    from highhx.agent import running

    candidates = running.running()
    if session_id:
        candidates = [a for a in candidates if a.session_id and session_id in a.session_id]
    elif not all_agents:
        candidates = [a for a in candidates if a.root == str(app.root)]
    results = [
        {"pid": a.pid, "session_id": a.session_id, "root": a.root, "stopped": running.stop(a)} for a in candidates
    ]
    out = app.output

    def render() -> None:
        if not results:
            out.info("No running agents" + ("" if all_agents else " in this project (use --all)") + ".")
        for r in results:
            (out.success if r["stopped"] else out.error)(
                f"{'Stopped' if r['stopped'] else 'Could not stop'} agent pid {r['pid']} ({r['session_id'] or '-'})"
            )

    out.emit({"stopped": results}, render)
    return 0 if all(r["stopped"] for r in results) else 1
