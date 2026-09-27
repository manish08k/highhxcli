"""highhx login / highhx logout"""

from __future__ import annotations

import sys
import webbrowser

import click
from rich.markup import escape

from highhx.cloud import credentials
from highhx.commands import App, pass_app
from highhx.core.errors import UsageError
from highhx.ui.progress import spinner


def _signed_in_message(app: App, email: str, plan_name: str, is_pro: bool) -> None:
    out = app.output
    out.success(f"Signed in as {email} ({plan_name})")
    if is_pro:
        out.info("Start the AI developer agent with `highhx agent`.")
    else:
        out.note("You are on HighhX Free — every CLI command works. `highhx account upgrade` unlocks `highhx agent`.")


@click.command("login", short_help="Sign in to your HighhX account (browser or token).")
@click.option("--with-token", is_flag=True, help="Read an API token from stdin (CI, headless machines).")
@click.option("--api-url", metavar="URL", help="HighhX platform URL (self-hosted / development).")
@click.option("--no-browser", is_flag=True, help="Print the sign-in link instead of opening a browser.")
@pass_app
def login(app: App, with_token: bool, api_url: str | None, no_browser: bool) -> int:
    """Sign in with the browser (device code), or with an API token from stdin:

    \b
      highhx login
      echo "$HIGHHX_TOKEN" | highhx login --with-token

    New users create their account in the same browser flow. Credentials are
    stored in your user config directory (mode 0600), never in the project.
    HIGHHX_TOKEN in the environment overrides stored credentials.
    """
    if api_url:
        creds = credentials.stored()
        creds.api_url = credentials.validate_api_url(api_url)
        credentials.save(creds)
    cloud = app.cloud
    out = app.output
    if with_token:
        token = sys.stdin.readline().strip()
        if not token:
            raise UsageError("No token on stdin.", hint='echo "$TOKEN" | highhx login --with-token')
        account = cloud.login_with_token(token)
    else:
        if not app.options.is_interactive() and not no_browser:
            raise UsageError(
                "Browser sign-in needs an interactive terminal.",
                hint="Use `highhx login --with-token` (token on stdin) or set HIGHHX_TOKEN.",
            )
        code = cloud.start_device_login()
        out.plain()
        out.markup(f"  Open [bold]{escape(code.verification_uri)}[/bold] and enter the code:")
        out.plain()
        out.markup(f"      [bold magenta]{escape(code.user_code)}[/bold magenta]")
        out.plain()
        if not no_browser:
            try:
                webbrowser.open(code.verification_uri_complete)
            except webbrowser.Error:
                pass
        with spinner(out.console, "Waiting for you to approve in the browser…", enabled=out.human):
            account = cloud.poll_device_login(code, cancel=app.ctx.cancel)
    app.output.emit(
        {"signed_in": True, **account.to_dict()},
        lambda: _signed_in_message(app, account.email, account.plan.name, account.is_pro),
    )
    return 0


@click.command("logout", short_help="Sign out and forget stored credentials.")
@pass_app
def logout(app: App) -> int:
    """Revoke this machine's token on the platform and delete it locally."""
    removed = app.cloud.logout()
    out = app.output
    out.emit(
        {"signed_out": removed},
        lambda: out.success("Signed out.") if removed else out.info("You were not signed in."),
    )
    return 0
