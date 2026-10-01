"""highhx mcp — the external MCP servers mounted for the HighhX agent: their state and tools."""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.integrations.mcp_client import McpManager, load_servers


def _manager(app: App) -> McpManager:
    return McpManager(load_servers(app.config.raw if app.initialized else None))


@click.group("mcp", cls=DefaultGroup, default_command="status", short_help="External MCP servers for the agent.")
def mcp() -> None:
    """MCP servers configured under `mcp.servers` in .highhx/config.yaml (or the JSON file named
    by HIGHHX_MCP_CONFIG, in the {"mcpServers": …} format). The HighhX agent mounts their tools as
    mcp__<server>__<tool>; each call is approved and audited like the agent's own actions.
    To serve HighhX's own computer tools to an MCP client, see `highhx computer mcp`."""


@mcp.command("status", short_help="Connect to each server and show its state.")
@pass_app
def mcp_status(app: App) -> int:
    """Start each configured server, negotiate MCP, list its tools, and show what works."""
    manager = _manager(app)
    out = app.output
    try:
        manager.connect_all()
        rows = manager.status()
    finally:
        manager.close()
    out.emit(
        {"servers": rows},
        lambda: out.table(["server", "state", "tools", "detail"], [(r["server"], r["state"], r["tools"], r["detail"]) for r in rows])
        if rows
        else out.info("No MCP servers are configured (mcp.servers in .highhx/config.yaml, or HIGHHX_MCP_CONFIG)."),
    )
    return 0 if all(r["state"] in ("connected", "disabled") for r in rows) else 1


@mcp.command("tools", short_help="The tools each server offers (as the agent sees them).")
@click.argument("server", required=False)
@pass_app
def mcp_tools(app: App, server: str | None) -> int:
    """List the tools of every server (or SERVER), with the name the agent calls them by."""
    from highhx.agent.tools.mcp import tool_name

    manager = _manager(app)
    out = app.output
    try:
        problems = manager.connect_all()
        tools = [t for t in manager.tools() if server in (None, t.server)]
    finally:
        manager.close()
    rows = [
        {
            "tool": tool_name(t.server, t.name),
            "server": t.server,
            "read_only": t.read_only,
            "description": " ".join(t.description.split())[:100],
        }
        for t in tools
    ]
    for problem in problems:
        out.warn(problem)
    out.emit(
        {"tools": rows, "problems": problems},
        lambda: out.table(
            ["tool", "reads only", "description"], [(r["tool"], "yes" if r["read_only"] else "no", r["description"]) for r in rows]
        )
        if rows
        else out.info("No tools (no server is connected)."),
    )
    return 0 if not problems else 1
