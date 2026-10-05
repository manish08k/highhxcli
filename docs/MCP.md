# MCP

HighhX exposes its tools to MCP clients (JSON-RPC 2.0 over stdio), and mounts external MCP
servers for its own agent.

## Serving HighhX's tools

```text
highhx mcp serve [--toolset desktop] [--toolset runtime] [--mode ask|read-only|auto-edit] [--allow TOOL …]
highhx computer mcp [--toolset …]          # the desktop toolset by default (unchanged)
claude mcp add highhx -- highhx mcp serve
```

| Toolset | Tools |
|---|---|
| `desktop` | `observe`, `screenshot`, `windows`, `apps`, `element_at`, `verify`, `launch`, `focus`, `click`, `click_at`, `move`, `mouse_button`, `drag`, `scroll`, `type`, `press`, `hotkey`, `menu`, `window`, `quit`, `clipboard_read`, `clipboard_write` (each `computer.<name>`) |
| `runtime` | `computer_state`, `browser_open`/`click`/`fill`/`select`/`press`/`scroll`/`extract`/`back`, `android_devices`/`observe`/`tap`/`type`/`swipe`/`key`/`launch`/`back`/`home`, `sandbox_create`/`list`/`exec`/`patch`/`destroy`, `api_request` |

**There is no second executor.** Each tool call becomes an `ActionRequest` through the action
protocol and the one `ActionExecutor`: risk classification, `policies.yaml`, approval,
verification, audit (source `mcp`), with `tool.started` / `tool.completed` / `tool.failed`
events and a trace id of its own. Nobody can be asked over stdio, so an action that needs
approval is refused, unless the person who starts the server pre-approves non-critical actions
with `--yes`. Blocked and critical actions are refused regardless. `--mode read-only` offers
read tools only, and `--allow` restricts the server to a bounded set. The result includes the
outcome (success, unknown, failed), the risk and the trace id.

## External MCP servers (for the agent)

```text
highhx mcp status       # connect to each configured server and show its state
highhx mcp tools [SERVER]
```

Configured servers are mounted for the HighhX Pro agent; their tools go through the agent's
permission layer.

## MCP in workflows (October 2026 phase)

- `mcp.call {server, tool, arguments}`: one tool call through the executor (medium risk, policy name
  `mcp:SERVER:TOOL`, so `policies.yaml` can deny a tool). Results are marked untrusted data; events
  `tool.started`, `tool.completed`, `tool.failed`.
- `mcp.resources {server[, uri]}`: list a server's resources or read one (read-only, untrusted).
