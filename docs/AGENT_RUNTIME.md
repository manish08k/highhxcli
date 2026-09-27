# The agent runtime (HighhX Pro)

User guide: [agent.md](agent.md). This document describes how the runtime works.

## Components

| Component | Code | Responsibility |
|---|---|---|
| Launch | `agent/launch.py` | Capabilities, attaching the agent session, event log, session id |
| Session | `agent/session.py` | The loop, retries, cancellation, persistence, state machine |
| Provider | `agent/model/platform.py` | The HighhX gateway: streamed, resumable, idempotent requests |
| Tools | `agent/tools/` | Typed tools over HighhX services, each through the action gate |
| `run_actions` | `agent/tools/actions.py` | Structured action graphs executed by the action engine |
| Permissions | `agent/permissions.py` | Confinement, approval modes, grants, audit |
| Planner | `agent/planner.py` | `propose_plan` / `update_plan` |
| Context & memory | `agent/context.py`, `agent/memory.py`, `actions/context.py` | Project facts, `.highhx/memory.md` |
| History & sync | `agent/history.py`, `agent/sync.py` | Transcripts (local), metadata (account) |
| UI | `agent/repl.py`, `agent/ui.py` | The shared interactive session |

No class does everything: the session drives the loop; tools, permissions, the executor,
the provider and the UI are separate objects with narrow interfaces (`AgentUI`, `Tool`,
`ModelProvider`, `GatePrompter`).

## Lifecycle of a request

```text
understand   system prompt: core rules, approval mode, project context, memory, instructions
inspect      tools: project_overview, list_files, read_file, search_code, git_status/diff/log …
plan         propose_plan → the person approves or revises → update_plan as steps progress
execute      edit_file/write_file (diffs), run_tests, run_checks, build, run_command, git_*,
             deploy, run_workflow, computer_* — or run_actions: one validated action graph
observe      tool results (redacted, framed as untrusted data) go back to the model
verify       tests/checks re-run; file writes read back; UI re-observed
recover      failed step → the model re-plans; denied/blocked → it must change course
summarise    final answer: what changed, what was verified, what is left
```

## Autonomous tasks: done means verified

A request that states when it is done — *"fix the login bug and make sure all tests pass"* —
runs as a **task** (`agent/tasks.py`). So does `/task <goal> [--verify test,check,build]
[--attempts N]` and, headless, `highhx agent --verify test "goal"` (exit 0 only when verified;
`--json` prints the report).

```text
definition of done   derived deterministically from the words: tests pass → project.test,
                     checks/lint pass → project.check, builds → project.build
                     (a check the project has no command for is reported as not verifiable)
attempt 1            the agent's normal loop: inspect, plan, change (approved), run tests
verify               HighhX runs the checks itself, as actions — never the model's word
feedback             failing checks + the tail of their real output + the goal → the agent
attempt 2…N          bounded (default 3); each attempt is a normal, approved agent turn
report               verified · unverified · done · stopped · cancelled, checks, attempts,
                     changed files, tokens, time; recorded as a `task` in history and as
                     task.started / task.attempt / task.verified / task.completed events
```

`/status` shows the last task; `/retry` gives an unverified task another round;
`/changes` and `/undo` cover everything it changed.

## `run_actions`: the hybrid model

The model proposes; HighhX executes:

```json
{"actions": [
  {"id": "t1", "action": "project.test"},
  {"id": "w",  "action": "filesystem.write", "inputs": {"path": "src/a.py", "content": "…"}, "depends_on": ["t1"]},
  {"id": "t2", "action": "project.test", "depends_on": ["w"]},
  {"id": "d",  "action": "git.diff", "depends_on": ["t2"]}
], "rollback_on_failure": true}
```

The whole graph is validated before the first step; each step is rated by the policy
(never by the model), approved according to the table (the agent never pre-approves),
executed, verified and audited as actor `agent`. The tool offers only the actions the
account's plan features allow and never browser or desktop actions — those use the
computer-use tools, which the platform entitles by name.

## Reliability

* **Model calls:** a fresh idempotency key per attempt (metered once), bounded exponential
  backoff with jitter for retryable errors, a circuit breaker, resumable SSE streams with
  `Last-Event-ID` (replayed events are deduplicated, gaps detected).
* **Tools:** never retried automatically; per-tool timeouts; structured `error_code`
  (`invalid_input`, `denied`, `policy`, `timeout`, `failed` …) so the model can react.
* **Cancellation:** Ctrl+C cancels the model request (also on the platform), retries and
  running processes; `highhx agent stop` does the same from another terminal.
* **State:** `created → running ⇄ waiting_for_confirmation → completed | failed | cancelled
  → closed`, persisted; one process per session (lease).
* **Platform outage:** the session says so, offers the deterministic route for the request
  and reattaches the agent when the platform answers again.

## Events

`agent.started`, `agent.turn`, `agent.tool_call` (tool, ok, error code, seconds),
`agent.completed` (stopped reason, steps, changed files), plus the action events of every
`run_actions` step. See [OBSERVABILITY.md](OBSERVABILITY.md).

## Multi-agent

Not implemented. The runtime executes one agent per session; sub-agents would add
parallel model calls and a second approval stream without a demonstrated benefit for the
tasks HighhX targets today. The action graph already parallelises nothing that needs a
person's approval and keeps a single, auditable decision trail. See [ROADMAP.md](ROADMAP.md).
