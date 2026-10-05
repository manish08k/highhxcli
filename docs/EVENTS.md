# Events

Everything HighhX runs emits events on the application's in-process `EventBus`
([`core/events.py`](../src/highhx/core/events.py)). Each event carries:

- `name`, `timestamp`, `payload`;
- an **envelope** from `trace_context()`: `trace_id`, `session_id`, `task_id`, `step_id`,
  `action_id`, `execution_id`, `source` (`agent`, a specialist's name, `mcp`, `benchmark`,
  `workflow-replay` …), whichever are set where it is emitted.

Nested contexts add to the outer ones. Contexts do not leak across threads. Unknown envelope keys
are refused.

## Consumers

| Consumer | How |
|---|---|
| Event log (`highhx events`) | `actions/events.EventLog`: JSON Lines per day, redacted, with the envelope |
| Task traces (`highhx trace`) | `observability/tasktrace.TaskTraceRecorder` → `TraceStore` ([TRACES.md](TRACES.md)) |
| Live dashboard / `highhx tui` | `observability/stream.EventRecorder` → `ui/live.DashboardState` |
| Benchmarks | the trajectory and metrics of each run |
| Plugins, tests | `EventRecorder.attach(bus)` / `bus.subscribe("*", …)` |

A consumer that raises never breaks execution. Payloads given to consumers are redacted, and
typed text in action inputs is replaced by its length before the event is emitted.

## Names

| Area | Events |
|---|---|
| Task / agent | `task.started` · `task.completed` · `task.failed` · `agent.started` · `agent.planning` · `agent.reflection` · `agent.completed` |
| Plan | `plan.created` · `plan.updated` (items, the current step and its status) |
| Actions | `action.planned` (= action.proposed: risk, approval) · `approval.requested` (= approval.required) · `approval.granted` · `approval.denied` · `action.started` · `action.retry` (= action.retrying) · `action.completed` · `action.failed` · `action.compensated` |
| Perception / grounding | `observation.created` · `grounding.started` · `grounding.attempt` · `grounding.completed` · `selector.healed` |
| Verification / recovery | `verification.started` · `verification.completed` · `verification.failed` · `recovery.started` · `recovery.completed` |
| Checkpoints | `checkpoint.created` · `checkpoint.resumed` |
| Tools (MCP) | `tool.started` · `tool.completed` · `tool.failed` |
| Runtimes | `sandbox.created` · `sandbox.exec` · `sandbox.destroyed` · `runtime.started` · `runtime.stopped` |
| Models | `model.usage` (tokens, cost when reported) |
| Network | `network.observed` (the sanitized requests a browser action caused: method, URL without query values, status, failures) |
| Benchmarks | `benchmark.started` · `benchmark.task` · `benchmark.completed` |
| Existing | `session.*`, `intent.*`, `workflow.*`, `agent.turn`, `agent.tool_call`, `voice.heard`, `computer.*` (the vision operator), `mcp.connected` |

The pre-existing names `action.planned`, `approval.requested` and `action.retry` are kept for
compatibility; the table shows the names the specification uses for them.

## Canonical names (October 2026 phase)

Every record carries `canonical`: the older names map to the spec's vocabulary
(`approval.requested` → `approval.required`, `action.retry` → `retry.started`, `agent.planning` →
`planning.started`, `plan.created` → `planning.completed`, `sandbox.created` → `sandbox.started`,
`sandbox.exec` → `sandbox.completed`, `computer.model.*` → `model.*`). New events: `task.paused`,
`task.resumed`, `task.cancelled`, `model.request|response|error`, `grounding.failed`,
`browser.started|navigation|crash|connection_lost|recovered|tab|download`, `computer.input`,
`computer.screenshot`, `android.action`, `sandbox.blocked`, `artifact.created`, `agent.delegated`,
`attempt.completed`, `approval.required|approved|rejected|modified|deferred|expired`.
