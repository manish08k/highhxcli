"""highhx web"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("web", short_help="The web console: tasks, live view, approvals, history (127.0.0.1 only).")
@click.option("--port", type=click.IntRange(0, 65535), default=0, help="Port on 127.0.0.1 (default: a free one).")
@click.option("--headed", is_flag=True, help="Show the browser window (default: headless when there is no display).")
@pass_app
def web(app: App, port: int, headed: bool) -> int:
    """Open the HighhX web console on this computer: start tasks, watch the live view, the plan,
    actions, network evidence and the event timeline, answer approvals, and browse trajectories
    and benchmarks. Everything runs through the same executor, policy and audit as the CLI.
    The address carries a one-time token and is reachable only from this computer. Ctrl-C stops it."""
    from highhx.ui.web import WebConsole

    console = WebConsole(app, port=port, headless=False if headed else None)
    console.start()
    out = app.output
    out.emit({"url": console.url, "port": console.port}, lambda: out.success(f"HighhX console: {console.url}"))
    try:
        while not app.ctx.cancel.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        console.close()
    return 0
