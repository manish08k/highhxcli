"""highhx account — your HighhX plan, usage, billing and AI settings."""

from __future__ import annotations

import webbrowser
from typing import Any

import click
from rich.box import ROUNDED
from rich.columns import Columns
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from highhx.cloud.account import Account
from highhx.cloud.plans import FREE, PLANS, PRO, Plan
from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.core.errors import AccountError


def _plan_panel(plan: Plan, *, current: bool) -> Panel:
    lines = [f"[dim]{escape(plan.tagline)}[/dim]", ""]
    lines += [f"[ok]✓[/ok] {escape(h)}" for h in plan.highlights]
    if plan.monthly_tokens:
        lines += ["", f"[dim]{plan.monthly_tokens // 1_000_000}M AI tokens / month included[/dim]"]
    title = f"[bold]{escape(plan.name)}[/bold]" + ("  [magenta](your plan)[/magenta]" if current else "")
    return Panel(
        "\n".join(lines),
        title=title,
        title_align="left",
        border_style="magenta" if plan.id == PRO else "dim",
        box=ROUNDED,
        padding=(1, 2),
    )


def render_plans(console: Console, current: str | None = None) -> None:
    console.print(
        Columns(
            [_plan_panel(PLANS[FREE], current=current == FREE), _plan_panel(PLANS[PRO], current=current == PRO)],
            equal=True,
            expand=True,
        )
    )


def render_upsell(console: Console, account: Account | None, *, signed_in: bool) -> None:
    """Shown when a Free user (or a signed-out user) starts the agent."""
    console.print()
    render_plans(console, account.plan.id if account else None)
    console.print()
    if not signed_in:
        console.print("[bold]Get started:[/bold] `highhx login` creates your account, then `highhx account upgrade`.")
    else:
        console.print("[bold]Upgrade:[/bold] `highhx account upgrade` — every Free command keeps working either way.")


def render_usage(console: Console, usage: dict[str, Any]) -> None:
    used = int(usage.get("tokens_used") or 0)
    included = int(usage.get("tokens_included") or 0)
    period = f"{str(usage.get('period_start', ''))[:10]} → {str(usage.get('period_end', ''))[:10]}"
    table = Table.grid(padding=(0, 3))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_row("Period", escape(period))
    if included:
        pct = min(100, round(used * 100 / included))
        filled = pct // 5
        bar = f"[magenta]{'█' * filled}[/magenta][dim]{'░' * (20 - filled)}[/dim]"
        table.add_row("AI tokens", f"{bar} {used:,} / {included:,} ({pct}%)")
    else:
        table.add_row("AI tokens", f"{used:,}")
    table.add_row("Requests", f"{int(usage.get('requests') or 0):,}")
    table.add_row("Agent sessions", f"{int(usage.get('sessions') or 0):,}")
    console.print(table)
    by_model = usage.get("by_model") or []
    if by_model:
        detail = Table(box=None, header_style="dim", pad_edge=False)
        for column in ("provider", "model", "requests", "input tokens", "output tokens"):
            detail.add_column(column)
        for row in by_model:
            detail.add_row(
                escape(str(row.get("provider"))),
                escape(str(row.get("model"))),
                f"{int(row.get('requests') or 0):,}",
                f"{int(row.get('input_tokens') or 0):,}",
                f"{int(row.get('output_tokens') or 0):,}",
            )
        console.print()
        console.print(detail)


@click.group(
    "account", cls=DefaultGroup, default_command="status", short_help="Your HighhX account, plan, usage and billing."
)
def account() -> None:
    """Your HighhX platform account. Sign in with `highhx login`.

    HighhX Free is the full developer CLI; HighhX Pro adds the AI developer agent
    (`highhx agent`)."""


@account.command("status", short_help="Who you are signed in as, your plan and usage.")
@pass_app
def account_status(app: App) -> int:
    """Show the signed-in account, its plan and features, and this period's AI usage."""
    cloud = app.cloud
    out = app.output
    if not cloud.signed_in:

        def signed_out() -> None:
            out.info("Not signed in — you are using HighhX Free (every CLI command works).")
            out.note("Sign in with `highhx login` to use HighhX Pro.")

        out.emit({"signed_in": False, "plan": FREE, "api_url": cloud.api_url}, signed_out)
        return 0
    acct = cloud.account()

    def render() -> None:
        pro = acct.is_pro
        out.kv(
            {
                "account": f"{acct.email}" + (f" ({acct.name})" if acct.name else ""),
                "plan": acct.plan.name,
                "subscription": acct.subscription.get("status") or ("active" if pro else "-"),
                "renews": str(acct.subscription.get("current_period_end") or "-")[:10],
                "platform": cloud.api_url,
            }
        )
        if acct.cached:
            out.warn("The HighhX platform is unreachable; showing cached account details.")
        out.plain()
        if pro:
            out.success("HighhX Pro: run `highhx agent` in any project.")
        else:
            out.info("HighhX Free: the full developer CLI. `highhx account upgrade` unlocks the AI developer agent.")
        usage = acct.usage
        if usage:
            out.plain()
            render_usage(out.console, usage)

    out.emit({"signed_in": True, "api_url": cloud.api_url, **acct.to_dict()}, render)
    return 0


@account.command("plans", short_help="Compare HighhX Free and HighhX Pro.")
@pass_app
def account_plans(app: App) -> int:
    """What each plan includes. No account needed."""
    current = None
    if app.cloud.signed_in:
        try:
            current = app.cloud.account().plan.id
        except AccountError:
            current = None
    app.output.emit(
        {"plans": [p.to_dict() for p in PLANS.values()], "current": current},
        lambda: render_plans(app.output.console, current),
    )
    return 0


@account.command("usage", short_help="AI usage in the current billing period.")
@pass_app
def account_usage(app: App) -> int:
    """Tokens and requests used through the HighhX AI gateway this period, by model."""
    usage = app.cloud.usage()
    app.output.emit(usage, lambda: render_usage(app.output.console, usage))
    return 0


def _open(app: App, url: str, what: str, no_browser: bool) -> None:
    out = app.output
    out.info(f"{what}: {url}")
    if not no_browser and app.options.is_interactive():
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass


@account.command("upgrade", short_help="Upgrade to HighhX Pro (opens secure checkout).")
@click.option("--no-browser", is_flag=True, help="Print the checkout link instead of opening a browser.")
@pass_app
def account_upgrade(app: App, no_browser: bool) -> int:
    """Start a HighhX Pro subscription through the platform's secure checkout."""
    acct = app.cloud.account()
    if acct.is_pro:
        app.output.emit({"plan": PRO, "url": None}, lambda: app.output.success("You already have HighhX Pro."))
        return 0
    url = app.cloud.checkout_url(PRO)
    if app.options.json:
        app.output.json({"plan": acct.plan.id, "url": url})
        return 0
    _open(app, url, "Complete your upgrade in the browser", no_browser)
    app.output.note("Your plan updates as soon as checkout completes — then run `highhx agent`.")
    return 0


@account.command("billing", short_help="Manage your subscription, invoices and payment method.")
@click.option("--no-browser", is_flag=True, help="Print the link instead of opening a browser.")
@pass_app
def account_billing(app: App, no_browser: bool) -> int:
    """Open the billing portal (change or cancel your subscription, download invoices)."""
    url = app.cloud.billing_portal_url()
    if app.options.json:
        app.output.json({"url": url})
        return 0
    _open(app, url, "Billing portal", no_browser)
    return 0


@account.command("settings", short_help="Default AI provider, model and approval mode for the agent.")
@click.option("--provider", type=click.Choice(["highhx", "anthropic", "openai", "gemini"]), help="Default AI provider.")
@click.option("--model", metavar="MODEL", help="Default model (e.g. claude-opus-5, gpt-5, gemini-2.5-pro).")
@click.option("--approval", type=click.Choice(["ask", "auto-edit", "read-only"]), help="Default approval mode.")
@click.option(
    "--upstream",
    type=click.Choice(["anthropic", "openai", "gemini"]),
    help="Upstream used by the managed HighhX provider.",
)
@click.option("--sync/--no-sync", "sync", default=None, help="Sync agent session metadata to your account.")
@pass_app
def account_settings(
    app: App, provider: str | None, model: str | None, approval: str | None, upstream: str | None, sync: bool | None
) -> int:
    """Show or change account-wide agent settings (project `agent:` config and flags override them)."""
    changes: dict[str, Any] = {}
    for key, value in (
        ("provider", provider),
        ("model", model),
        ("approval", approval),
        ("upstream", upstream),
        ("sync_sessions", sync),
    ):
        if value is not None:
            changes[key] = value
    if changes:
        data = app.cloud.update_settings({"agent": changes})
        app.output.emit(
            data, lambda: app.output.success("Settings saved: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
        )
        return 0
    acct = app.cloud.account()
    agent_settings = dict(acct.settings.get("agent") or {})
    app.output.emit(
        {"agent": agent_settings},
        lambda: app.output.kv(
            {
                "provider": agent_settings.get("provider") or "highhx (default)",
                "upstream": agent_settings.get("upstream") or "platform default",
                "model": agent_settings.get("model") or "provider default",
                "approval": agent_settings.get("approval") or "ask (default)",
                "sync sessions": agent_settings.get("sync_sessions", True),
            }
        ),
    )
    return 0
