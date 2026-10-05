"""Workflow blocks that are actions: an MCP tool call, saving an artifact, a nested agent task.
Each is a catalog action, so the executor classifies, approves, audits and records it like any
other — and everything they do in turn (the agent's steps) goes through the executor again."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from highhx.actions.spec import ActionContext, ActionResult, Inputs
from highhx.agent.tools.base import ToolError

MAX_TOOL_TEXT = 20_000


def mcp_call(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Call one tool on a configured MCP server (``mcp.servers`` or ``HIGHHX_MCP_CONFIG``). The
    result is data from another program — marked untrusted, never followed as instructions."""
    from highhx.integrations.mcp_client import McpClient, McpError, load_servers

    server_name, tool = str(inputs["server"]), str(inputs["tool"])
    servers = {s.name: s for s in load_servers(ctx.app.config.raw if ctx.app.initialized else {})}
    config = servers.get(server_name)
    if config is None or not config.enabled:
        raise ToolError(f"No enabled MCP server named {server_name!r} (configure mcp.servers).")
    client = McpClient(config)
    events = ctx.app.ctx.events
    try:
        client.connect()
        offered = {t.name: t for t in client.tools}
        if tool not in offered:
            raise ToolError(f"{server_name} has no tool {tool!r} (it offers: {', '.join(sorted(offered)) or 'none'}).")
        events.emit("tool.started", tool=f"{server_name}.{tool}", source="mcp")
        result = client.call(tool, dict(inputs.get("arguments") or {}))
        events.emit("tool.completed" if not result.error else "tool.failed", tool=f"{server_name}.{tool}", source="mcp")
    except McpError as exc:
        events.emit("tool.failed", tool=f"{server_name}.{tool}", source="mcp", error=exc.message[:200])
        outcome = "unknown (do not repeat it; check its effect)" if exc.unknown_outcome else "failed"
        return ActionResult(False, error=f"{server_name}.{tool} {outcome}: {exc.message}", retryable=False)
    finally:
        client.close()
    text = result.text[:MAX_TOOL_TEXT]
    return ActionResult(
        not result.error,
        output={
            "text": text,
            "structured": result.structured,
            "untrusted": True,
            "truncated": len(result.text) > MAX_TOOL_TEXT,
            "tool": f"{server_name}.{tool}",
            "read_only": offered[tool].read_only,
        },
        summary=f"{server_name}.{tool}" + (" (error)" if result.error else ""),
        error=text[:200] if result.error else "",
        retryable=False,
    )


def artifact_save(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    from highhx.actions.handlers.files import _confine
    from highhx.artifacts import ArtifactStore

    path = _confine(ctx, str(inputs["path"]), must_exist=True)
    artifact = ArtifactStore.for_app(ctx.app).add(
        Path(path),
        kind=str(inputs.get("kind") or "generated"),
        name=str(inputs.get("name") or ""),
        action="artifact.save",
        retention_days=int(inputs.get("retention_days") or 30),
    )
    return ActionResult(
        True, output=artifact.to_dict(), summary=f"saved {artifact.name} as {artifact.id}", verified=True
    )


def agent_run(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """A nested agent task: its own trajectory, every one of its actions through this executor."""
    from highhx.agent.loop import AgentLoop, AgentTask, ResolverPlanner, ScriptedPlanner
    from highhx.trajectories import TrajectoryStore

    goal = str(inputs["goal"])
    steps = inputs.get("steps")
    executor: Any = ctx.executor
    planner = ScriptedPlanner(list(steps)) if steps else ResolverPlanner(ctx.app, goal, executor=executor)
    task = AgentTask(
        goal,
        surface=str(inputs.get("surface") or "auto"),
        max_steps=int(inputs.get("max_steps") or 20),
        timeout=float(inputs.get("timeout") or 600),
    )
    result = AgentLoop(executor, planner, store=TrajectoryStore.for_app(ctx.app), agent="workflow").run(task)
    ok = str(result.status) == "completed"
    return ActionResult(
        ok,
        output={
            "status": str(result.status),
            "summary": result.summary,
            "task_id": result.trajectory.id,
            "steps": len(result.trajectory.steps),
        },
        summary=f"agent task {result.trajectory.id}: {result.status}",
        verified=ok,
        error="" if ok else result.summary,
        retryable=False,
    )


def mcp_resources(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """List a configured MCP server's resources, or read one (``uri``). Read-only; the contents are
    untrusted data."""
    from highhx.integrations.mcp_client import McpClient, McpError, load_servers

    server_name = str(inputs["server"])
    config = {s.name: s for s in load_servers(ctx.app.config.raw if ctx.app.initialized else {})}.get(server_name)
    if config is None or not config.enabled:
        raise ToolError(f"No enabled MCP server named {server_name!r} (configure mcp.servers).")
    client = McpClient(config)
    try:
        client.connect()
        if inputs.get("uri"):
            contents = client.read_resource(str(inputs["uri"]))
            return ActionResult(
                True,
                output={"contents": [{**c, "text": c["text"][:MAX_TOOL_TEXT]} for c in contents], "untrusted": True},
                summary=f"read {inputs['uri']}",
            )
        listed = client.resources()
        return ActionResult(True, output={"resources": listed}, summary=f"{len(listed)} resource(s) on {server_name}")
    except McpError as exc:
        return ActionResult(False, error=exc.message)
    finally:
        client.close()
