# Observability

Everything HighhX runs leaves four kinds of records, each redacted before it is written.

| Record | Where | Read with |
|---|---|---|
| **History** — every operation (command, action, workflow, agent turn) with its steps, exit codes, durations, errors, outputs | `.highhx/state/highhx.db` (SQLite; user data directory outside a project) | `highhx history`, `highhx workflow runs`, `highhx workflow inspect RUN_ID` |
| **Logs** — full output of each execution | `.highhx/logs/<execution-id>.log` | `highhx logs [ID]` |
| **Audit** — every safety decision: action, actor, tool, risk, categories, decision (allowed / confirmed / denied / blocked), ticket, outcome, verification | state database | `highhx audit` |
| **Events** — the structured stream below | `<data dir>/events/YYYY-MM-DD.jsonl` | `highhx events [--session ID] [--type PREFIX]`, `/history` |

Traces (spans per operation and workflow step) and metrics are recorded in the state
database as well (`highhx trace`, `highhx report`).

## Events

| Event | Emitted when | Main fields |
|---|---|---|
| `session.started` / `session.ended` | the interactive session starts / ends | session, tier, connection, agent |
| `intent.resolved` / `intent.unresolved` | plain language is (not) resolved | text, rule, actions / capability |
| `action.planned` | an action is validated and rated | action, inputs (content omitted), risk, approval, asks, blocked, reasons, actor |
| `approval.requested` / `approval.granted` / `approval.denied` | approval is needed / given / refused | action, risk, mode (`preapproved`, `plan`) |
| `action.started` / `action.retry` | an attempt starts / is retried | action, attempt, delay, error |
| `action.completed` / `action.failed` | the action ends | action, summary, seconds / status, error |
| `action.compensated` | an action is undone | action, summary |
| `workflow.started` / `workflow.finished`, `step.started` / `step.finished`, `workflow.rollback` | workflow execution | workflow, step, status |
| `agent.started`, `agent.turn`, `agent.tool_call`, `agent.completed` | the Pro agent | session, tool, ok, error_code, stopped, steps |
| `voice.enabled`, `voice.heard` | voice mode | engines, text |

Every line is a JSON object with `ts`, `event` and `session`; values are plain scalars and
short lists. File contents, environment values and approval tickets are never part of an
event; everything passes the redactor (token patterns, secret-looking variable values,
credentials in URLs, private keys).

## Using it

```text
highhx events --type action. -n 20        # the last 20 action events
highhx events --session s-1a2b3c4d5e6f     # one session
highhx audit --limit 20                    # recent safety decisions
highhx history --kind workflow             # workflow runs
```

In the session, `/history` shows what the session ran; `/changes` which files changed.
