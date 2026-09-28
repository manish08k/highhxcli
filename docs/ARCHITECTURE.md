# Architecture

HighhX is layered so that every side effect passes through one place where safety
rules are enforced. Product-level behaviour is specified in [PRODUCT_SPEC.md](PRODUCT_SPEC.md).

```text
interfaces           highhx (session) · highhx "request" · highhx <command> · workflows · voice · the Pro agent
    ↓
language/            parser (normalise, clauses) · grammar (verb registry) · entities · targets (YAML registry)
decision/            deterministic: request → Decision (route, intent, target, entities, plan) · risk classes
                     advanced: the Pro-only gate for JEv / advanced reasoning
plans/               JSON action plan (schema v1, validation) · planner · runner (verify, stop on failure)
    ↓
actions/             resolver (developer rules) · catalog (85 actions) · policy (risk, approval)
                     · executor (validate → classify → approve → run → verify → record) · graphs
    ↓                ├─ automation/engine/  bridge protocol → C#/.NET engine (engine/dotnet) or Python engine
                     ├─ computer/           HighhX browser (DevTools) · computer runtime (element safety)
                     └─ verification/       strategies: page_open, media_playing, file_exists …
    ↓
safety/              classifier · action gate · confirmation tickets · audit log
    ↓
commands/            thin CLI modules: parse args → call a service → render
    ↓
domain services      project, workflows, environment, dependencies, testing, building,
                     git, release, deployment, security, services, database, plugins,
                     automation, diagnostics
    ↓
core/engine.py       policy → risk classification → approval → dry-run → execute → record
    ↓
core/executor.py     retries, backoff, timeouts, cancellation, parallelism
    ↓
execution/           subprocesses, shells, environment, isolation, cancellation tokens
    ↓
integrations/        docker, kubernetes, ssh, terraform, databases, cloud (plugin registry)
```

Cross-cutting packages: `approvals/` (risk levels, rules, approval manager),
`policy/` (policies.yaml), `storage/` (SQLite history, logs, cache), `observability/`
(logging with redaction, events, metrics, tracing), `config/`, `ui/`, `utils/`.

## Request flow

Every entry point ends in the same executor:

```text
typed text ──▶ deterministic resolver ──▶ JSON plan ──▶ plan runner ─┐   (Free; traced)
highhx "…" / highhx do ─▶ deterministic resolver ──▶ plan runner ───┤
/run, highhx actions run ────────────────────────────────┤
workflow `action:` step ─────────────────────────────────┼─▶ ActionExecutor.plan ─▶ execute
agent run_actions graph ─▶ validate_graph ───────────────┤      (actor = user | agent)
voice ──▶ local speech-to-text ──▶ confirmed text ──▶ router
!command ──▶ shell.run ───────────────────────────────────┘

execute: ActionGate.authorize (classifier + catalog floor + policy + approval, ticket)
       → gate.executing (ticket redeemed, audit) → engine.operation (history)
       → handler (timeout, cancellation, idempotent retries) → verify → events
```

| Package | Responsibility |
|---|---|
| `actions/spec.py` | `ActionSpec` (name, schema, outputs, risk floor, kind, permissions, timeout, retry, idempotence, verification, compensation, agent feature), `ActionResult`, `ActionContext` |
| `actions/catalog.py` | The built-in catalog and per-project plugin actions |
| `actions/policy.py` | Five risk levels, the approval table, action-level rules (e.g. `rm -rf` → critical) |
| `actions/executor.py` | Planning, execution, compensation, action graphs |
| `actions/resolver.py` | Deterministic grammar with entities from the project |
| `actions/handlers/` | Command-backed actions (`delegate`), files, git, browser, project detection |
| `actions/context.py` | Structured project context (names only, never secret values) |
| `actions/events.py` | Event names, the JSON Lines event log |
| `language/` | Parser, verb grammar, entity extraction, target registry (`data/targets.yaml` + the user's `targets.yaml`) |
| `decision/deterministic.py`, `decision/risk.py` | Free: the deterministic decision (route local · unknown · pro), risk classes |
| `decision/advanced.py` | The Pro-only gate for JEv / advanced decision-model reasoning (today fulfilled by the Pro agent) |
| `plans/schema.py`, `plans/planner.py`, `plans/runner.py`, `plans/request.py` | JSON action plans: schema and validation, building, verified execution, one-shot requests |
| `verification/strategies.py` | Named post-conditions for plan steps |
| `automation/engine/` | The automation bridge: protocol v1, engine selection, Python engine, .NET engine client, a desktop provider for the computer runtime |
| `engine/dotnet/` | The C#/.NET automation engine (`highhx-automation`) |
| `observability/runs.py` | Automation run traces and metrics (`highhx runs`) |
| `agent/router.py` | Free routing: the deterministic plan, or the Pro capability with local alternatives |
| `agent/repl.py`, `agent/ui.py`, `agent/input.py` | The interactive session (one UI for Free and Pro) |
| `agent/launch.py` | Session start: capabilities, agent attachment, event log |
| `agent/tools/actions.py` | `run_actions`: the agent's structured action graphs |
| `cloud/capabilities.py` | Capabilities from the platform-reported plan features |
| `workflows/` | Parser, validator, DAG scheduler, engine (action steps, rollback, resume), running registry |
| `voice/` | Engines (recorders, local speech-to-text, speech) and voice mode |

## The HighhX browser

`computer/browser.py` drives a Chromium-family browser on its own profile. One place for each
concern:

| Module | Responsibility |
|---|---|
| `computer/cdp.py` | Transport: **one** WebSocket to the browser endpoint, a flat session per tab (`Target.attachToTarget`). Errors say whether a command can have run: *not delivered* (safe to resend) vs *outcome unknown* (timeout, crash, tab closed, connection dropped). Command ids are unique, so a late answer is ignored, and a timeout does not tear the connection down. |
| `computer/tabs.py` | The tab registry, kept current from target events: stable target ids, opener, URL/title, when HighhX last used each tab. Tabs are always chosen from it — never "the first tab listed". |
| `ChromeBrowser` | Lifecycle (start, reconnect, restart a crashed or hung browser, stop a stalled load), tab selection (the remembered working tab → the most recently used survivor → a new tab), and `_run` — the **single recovery policy** every action goes through. |

Every action has a retry class. *Safe* actions (reads, waits, screenshots, exact scrolls, exact
history jumps, navigation — verified first) are recovered and repeated, at most three times with
backoff. *Unsafe* actions (click, type, key, submit, upload, download, reload) are recovered and
**never repeated** once they may have run: HighhX looks at the page and reports what it found
(`OutcomeUnknownError`, audited as `unknown`). A command that was never delivered is always safe
to send again.

State transitions (`CONNECTED`, `PAGE_CLOSED`, `TARGET_CHANGED`, `CONNECTION_LOST`,
`BROWSER_CRASHED`, `BROWSER_HUNG`, `NAVIGATION_FAILED`, `RECOVERY_REQUIRED` …), dialog decisions
(alerts acknowledged; confirm/prompt/leave-page cancelled), followed or ignored popups and downloads
are journaled and attached to the action's audit record (`details.browser`).

## Composition root

`highhx.commands.App` builds every service lazily for one invocation — there is no
global state. Command modules receive it with `@pass_app`:

```python
@click.command("deploy")
@pass_app
def deploy(app: App, target: str | None) -> int:
    outcome = app.deployments.deploy(target)  # domain service
    app.output.emit(outcome.to_dict(), render)  # JSON or human output
    return 0 if outcome.ok else 1
```

## The engine

`Engine.run(spec)` is the only way domain code runs a side-effecting command:

1. **Policy** — `PolicyEngine.evaluate()`; `deny` raises `PolicyViolationError`,
   `require_approval` raises the risk and can make it non-bypassable.
2. **Risk** — `classify_command()` matches built-in and configured rules
   (e.g. `git push` → dangerous, `rm -rf /` → critical and non-bypassable).
3. **Approval** — `ApprovalManager` auto-approves up to `auto_approve`, honours `--yes`
   up to `yes_max_risk` for bypassable requests, prompts in a terminal and denies otherwise.
4. **Dry run** — returns a skipped result without executing.
5. **Execute** — `Executor` with streaming output, retries and timeouts.
6. **Record** — history (SQLite) and a redacted log file per execution.

`Engine.capture()` is for read-only queries (e.g. `git status`) and skips 1–4.
`Engine.operation(kind, name)` groups several commands into one history entry;
nested operations become steps.

## Workflow execution

```text
YAML → parser (schema) → validator (graph, expressions, commands)
     → DependencyGraph → DagScheduler ─┬→ ready steps in parallel (≤ max_parallel)
                                       ├→ results → decide next ready steps
                                       └→ fail-fast cancels running siblings
```

The scheduler evaluates each step's condition only after all of its dependencies
finished, so a dependent can never start early. Conditions and `${{ }}` variables
use a small expression parser (no `eval`).

## Storage

`.highhx/state/highhx.db` (SQLite, WAL) holds executions, steps, spans, deployments,
cache and key/value state; migrations are versioned in `storage/migrations.py`.
Logs are plain text files in `.highhx/logs/<execution-id>.log`. Outside an initialized
project, history goes to the user data directory.

## Extension points

- `plugins/interface.py` — `HighhXPlugin.register(api)` with `add_command`, `add_detector`,
  `add_deployment_strategy`, `add_cloud_provider`, `add_workflow_dir`, `add_template_dir`.
- `deployment/strategy.py` — `register_strategy(type, factory)`.
- `integrations/cloud` — provider registry for `type: plugin:<name>` targets.
- `templates/<stack>/` — project templates rendered by `highhx init`.

## The agent layer (HighhX Pro)

```text
highhx agent ─▶ repl / ui ─▶ session ─┬─▶ model provider ──▶ HighhX gateway │ Anthropic │ OpenAI │ Gemini
                                       │     (streamed events)
                                       └─▶ tools ─▶ permissions ─▶ domain services / Engine.run ─▶ history
```

| Module | Responsibility |
|---|---|
| `agent/session.py` | The loop: model → tool calls → results → model, step limits, retries, cancellation, persistence |
| `agent/model/` | `ModelProvider` protocol and streaming adapters; provider-neutral `messages` / `streaming` events |
| `agent/tools/` | Tools wrapping existing services (`TestRunner`, `Builder`, `GitManager`, scanner, `DeploymentManager` …) |
| `agent/permissions.py` | Path confinement, secret files, `agent:*` policy actions, approval modes and session grants |
| `agent/planner.py` | Plans (`propose_plan` / `update_plan`) and live progress |
| `agent/context.py`, `memory.py`, `prompts.py` | Project context, `.highhx/memory.md`, the system prompt |
| `agent/history.py`, `sync.py` | Transcripts in the state database (migration 3); metadata sync to the platform |
| `agent/ui.py`, `repl.py` | Rich terminal UI and slash commands |
| `cloud/` | Platform client (stdlib HTTP + SSE, protocol versioning, resumable streams), credentials, plans |
| `safety/` | Shared by Free and Pro: action descriptors, semantic classifier, confirmation tickets, action gate, audit log, untrusted-content framing |
| `computer/` | Computer use: semantic observations, action candidates, runtime (observe → act → verify), Chrome DevTools, macOS Accessibility, OCR, flows, deterministic intents |
| `agent/state.py`, `agent/running.py` | Session state machine; registry of running agents (kill switch) |
| `agent/model/resilience.py` | Backoff with jitter and a circuit breaker for model calls |

The session installs itself as the engine's output sink and approval prompter, so
engine prompts and command output flow through the agent UI instead of colliding with
it. Tools never spawn processes directly — they call the same services and `Engine.run`
as the commands do.

## The platform (`server/`)

A separate package (`highhx-platform`, FastAPI + SQLAlchemy) that imports the CLI's
plan definitions (`highhx.cloud.plans`), wire format (`highhx.cloud.sse`,
`highhx.agent.streaming`) and provider adapters, so both ends share one implementation.
See [platform.md](platform.md).

## Runtime choice

HighhX is a single Python runtime. A .NET worker for process orchestration was evaluated
against measurements on the real engine (macOS, Apple silicon, Python 3.14):

| Measurement | Before | After |
|---|---|---|
| raw `subprocess` spawn of `/usr/bin/true` | 1.8 ms | 1.8 ms |
| `Engine.run` (policy, risk, approval, execute, redaction) | 58.7 ms | 3.4 ms |
| 64-step workflow, 16 in parallel | 221 steps/s | 416 steps/s |

The overhead was a fixed 50 ms sleep in the process wait loop — a Python detail, fixed by
waiting on the process with a timeout (`execution/process.py`). The work HighhX orchestrates
is I/O-bound subprocesses and network calls, where a second runtime brings no measurable
gain but would cost every user a .NET runtime (or ~70 MB self-contained binaries per
platform), a versioned IPC protocol, and a second implementation of cancellation, redaction
and audit. The action handler contract (`ActionContext` → `ActionResult`) is
transport-agnostic, so a native worker can be added behind it if a real bottleneck (e.g.
Windows UI Automation) ever justifies one; see [ROADMAP.md](ROADMAP.md).
