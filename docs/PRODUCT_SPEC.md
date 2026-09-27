# HighhX product specification

The single source of truth for what HighhX is, how it behaves and why. Engineering detail
lives in the documents linked from each section; this one describes the product.

| Document | Covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Layers, packages, data flow |
| [ACTION_ENGINE.md](ACTION_ENGINE.md) | Action catalog, executor, graphs, compensation |
| [SAFETY_MODEL.md](SAFETY_MODEL.md) | Risk levels, approval rules, blocked actions, rollback |
| [SECURITY.md](SECURITY.md) | Threat model, secrets, confinement, plugins, data |
| [AGENT_RUNTIME.md](AGENT_RUNTIME.md) | The Pro agent loop and the AI gateway |
| [FREE_PRO.md](FREE_PRO.md) | The plan boundary and how it is enforced |
| [AUTOMATION.md](AUTOMATION.md) | Workflows, rollback, resume, cancel, schedules, triggers |
| [VOICE.md](VOICE.md) | Voice as an interface onto the same session |
| [OBSERVABILITY.md](OBSERVABILITY.md) | Events, history, audit, logs, traces |
| [PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md) | Plugins and their boundaries |
| [CLI_REFERENCE.md](CLI_REFERENCE.md) | The session and the most used commands |
| [ROADMAP.md](ROADMAP.md) | What is next, and what is deliberately not built |

## 1. What HighhX is

HighhX is a developer automation CLI. You run `highhx` in a project and describe what you
want. Everything HighhX does — whether you typed a command, a sentence, a workflow step or
the AI agent proposed it — is an **action** from one catalog, and every action runs through
one executor that rates its risk deterministically, asks for approval when the risk calls
for it, executes it with timeouts and cancellation, verifies the result, records it, and
can undo it where that is technically possible.

Two plans share that engine and one interface:

* **HighhX Free — deterministic developer automation.** No AI, no account, no network
  unless you ask for something that needs it. Plain language works when it maps to a known,
  fully specified action; workflows, shell commands, file and git operations, deployments
  with your own targets, browser automation with known targets.
* **HighhX Pro — deterministic automation + AI developer agent.** Everything in Free, plus
  an agent that understands open-ended requests, plans, inspects the repository, changes
  code, runs tools, recovers from failures and verifies its work — by proposing actions to
  the *same* executor. The model is reached only through the HighhX platform, which checks
  the plan on every request.

## 2. Core user experience

```text
$ highhx
  (Knight)  HighhX v0.6.0 · Developer command center · ~/code/shop · main • clean · Free • Local

❯ run the tests and then build
◉ run the tests  project.test · low
✓ run the tests — highhx test succeeded (2.1s)
◉ build the project  project.build · low
✓ build the project — highhx build succeeded (4.0s)

❯ /plan deploy staging
 #  action              inputs               risk  approval
 1  deployment.deploy   environment=staging  high  asks
/approve runs exactly this plan · /deny discards it

❯ fix the failing tests
╭─ HighhX Pro capability ─────────────────────────────╮
│ HighhX Pro can understand and execute this open-ended task.
│ AI debugging requires HighhX Pro.
│ Available locally, without AI:
│ 1  Run the tests          project.test
│ 2  Diagnose the project   security.diagnose
│ 3  Show recent logs       service.logs
╰─────────────────────────────────────────────────────╯
```

The loop is always the same: **understand → plan → show intent and risk → ask when required
→ execute → verify → summarise → record changes**. On Pro, "understand" and "plan" are done
by the agent for open-ended requests; everything after that is the same engine.

Session commands (full list in [CLI_REFERENCE.md](CLI_REFERENCE.md)), grouped by purpose:
run (`/plan`, `/approve`, `/deny`, `/retry`, `/run`), review (`/status`, `/history`,
`/changes`, `/undo`, `/doctor`), workflows (`/workflows`, `/workflow …`, `/resume`,
`/cancel`), project (`/init`, `/context`, `/tools`, `/config`, `/memory`), account & AI
(`/login`, `/account`, `/pro`, `/usage`, `/model`, `/mode`), session (`/voice`, `/clear`,
`/help`, `/quit`). `/status` is the hub: it lists what is waiting on you and the command that
acts on it.

Onboarding is four steps: `pip install highhxcli` → `highhx` (the first session shows
*Getting started*) → `/init` → a first request; `/login` adds Pro. Approval prompts always
show why they ask and exactly what will change (a diff for file writes). `!command` runs a shell command
as an action; `highhx <command>` runs any HighhX command inside the session.

## 3. Free capabilities

| Area | What works without AI |
|---|---|
| Plain language | A deterministic grammar (tests, checks, build, fix/format, dependencies, services, logs, git, docker, database, deploy with a named target, workflows, files, search, browser with URLs, known apps); compound requests when every part resolves |
| Actions | 73 catalog actions: project, filesystem, git, package, docker, database, service, browser, computer, deployment, security, workflow, shell |
| Files | Read, write, copy, move, delete, search — confined to the project, secret files refused, `.gitignore` respected, every change journaled for `/undo` |
| Git | status, diff, log, branch, checkout, commit, tag, push, pull, revert — commit/tag/checkout/pull can be undone |
| Shell | `!command` — classified by what it does, approval by risk, blocked when catastrophic |
| Workflows | YAML graphs of commands, actions and sub-workflows: dependencies, parallelism, conditions, retries, timeouts, approvals, rollback, resume, cancel, schedules, event triggers, git hooks, file watchers |
| Everything else | The full developer CLI: init, status, doctor, diagnose, repair, test, build, deps, env, release, deploy, rollback, services, docker, db, security scans, history, audit, events, plugins |
| Voice | Push-to-talk with a local speech-to-text engine; spoken requests go through the same deterministic resolver |

Free never guesses. A request it cannot resolve completely is not half-executed: it shows
what Pro would do and which local actions can do part of it.

## 4. Pro capabilities

Pro adds the AI developer agent to the same session: open-ended requests ("find why the API
is slow and fix it", "prepare this repository for production"), repository discovery,
planning with an approval step, multi-step execution through `run_actions` graphs and the
specialised tools (file edits with diffs, tests, checks, builds, git, deploys, workflows,
AI computer use), recovery and re-planning on failure, verification, project memory,
persistent sessions (`highhx agent --continue` / `--resume`) synced to the account, a choice
of upstream models through the gateway, and voice with natural-language understanding.

**Autonomous tasks.** When a request says when it is done ("…and make sure all tests pass"),
the agent works until **HighhX has verified it**: HighhX derives the checks from the words,
runs them itself after each attempt, sends failures (with their real output) back, and stops
after a bounded number of attempts with a report — verified or not. The model decides how to
reach the goal; HighhX decides whether it was reached. See [AGENT_RUNTIME.md](AGENT_RUNTIME.md).

Pro never adds an execution path: the agent's proposals are validated, rated, approved and
executed exactly like the user's own actions — with stricter approval (the agent can never
pre-approve anything, `--yes` does not apply to it).

## 5. Agent lifecycle (Pro)

```text
request → system prompt (project context, memory, mode) → model (streamed, via gateway)
  → tool calls → per call: validate input → classify → policy → approval → execute → verify → audit
  → results (framed as untrusted data) → model → … → final summary → session saved
```

Step limits, output limits, refusals, cancellation (Ctrl+C, `highhx agent stop`) and
provider failures end a turn cleanly; the conversation is kept. Model calls retry with
bounded exponential backoff and a circuit breaker; tools are never retried automatically.
Details: [AGENT_RUNTIME.md](AGENT_RUNTIME.md).

## 6. Intent resolution

`highhx.actions.resolver` turns text into actions with an ordered grammar. A request resolves
only when a rule matches the *whole* text and every entity is known: a configured service,
deploy target, environment or workflow, a file that exists, a URL, a quoted commit message.
Unknown entities mean no resolution — never a guess. Compound requests ("install deps, then
run the tests") resolve only when every part does. The resolver is pure and deterministic
(same text + project → same result) and reports which rule matched in the event log.

## 7. Planning

* **Free:** the resolver's steps are the plan. `/plan <request>` previews it with the
  effective risk and approval of each step, without running anything.
* **Pro:** the agent proposes a plan (`propose_plan`) for multi-step work and waits for your
  approval; `run_actions` graphs are validated in full — known actions, valid inputs, known
  dependencies, no cycles, allowed by the plan — before the first step runs.

## 8. Approval

One table decides ([SAFETY_MODEL.md](SAFETY_MODEL.md)): SAFE runs; LOW runs for you and asks
the agent in `ask` mode; MEDIUM and HIGH always ask (your own may be pre-approved with
`--yes` or by `/approve` of an exact preview); CRITICAL needs you to type `approve` in a
terminal and nothing pre-approves it; blocked actions never run. Approvals are bound to the
exact action by a single-use HMAC ticket, so what you approved is what runs.

## 9. Execution

The executor runs each action with a timeout, a cancellation token (Ctrl+C cancels the
running action, not the session), idempotency-aware retries (only idempotent actions, only
transient failures, exponential backoff), captured output, redaction and a history record.
Commands inside actions go through the engine's own policy and risk checks as well.

## 10. Verification

Actions declare how success is checked: file writes are read back and compared by SHA-256,
deletes are checked for absence, commands by exit code, git commits by the new HEAD, UI
actions by re-observing the page. A verification failure fails the action. The agent is
instructed not to report success it has not verified.

## 11. Rollback

Actions that can be undone declare a compensation (file writes, copies, moves, deletes,
commits, tags, checkouts, pulls). `/undo` restores the last file changes; action graphs and
workflows with `on_failure: rollback` undo completed steps newest-first when a later step
fails; a resumed workflow re-runs steps whose effect was rolled back. Things that cannot be
undone (a push, a sent message, a production deployment) say so and are rated accordingly.

## 12. Security model

Deterministic safety, never delegated to a model: structural classification of commands,
UI actions, files and deployments; project policy (`policies.yaml`); confinement to the
project; secret files never read or written by automation; redaction of everything printed,
logged or sent; untrusted-content framing for the agent; audit log. Details:
[SECURITY.md](SECURITY.md).

## 13. Automation architecture

Workflows are first-class DAGs of commands, actions and sub-workflows with dependencies,
conditions, retries with backoff, timeouts, approvals, failure handling, rollback,
resumability, cross-process cancellation, structured outputs, history and events.
Schedules (cron syntax, `highhx schedule run`), event triggers (`on:` / `highhx trigger`),
git hooks and file watchers start them. Details: [AUTOMATION.md](AUTOMATION.md).

## 14. Voice architecture

`voice → local speech-to-text → confirmed transcript → session.handle(text) → resolver or
agent → action engine → spoken summary`. Voice has no business logic of its own; it is
push-to-talk (the microphone is open only between two presses of Enter) and every
transcript is confirmed before anything runs. Details: [VOICE.md](VOICE.md).

## 15. Plugin architecture

Declarative plugins contribute commands, workflows, templates and detectors; trusted code
plugins may add commands, detectors, deployment strategies and cloud providers. Declared
plugin commands become catalog actions (`plugin.<plugin>.<command>`) with a risk floor that
can only be raised, never offered to the AI agent. Details: [PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md).

## 16. Session and history

Each interactive session has an id. Free sessions record their events; Pro sessions also
persist the transcript, plan and usage (resumable, one process at a time via a lease) and
sync metadata to the account. `highhx history` lists every operation with its steps;
`highhx audit` every safety decision; `highhx events` the structured event stream;
`highhx workflow runs` every workflow execution.

## 17. Observability

Structured events (`session.*`, `intent.*`, `action.*`, `approval.*`, `workflow.*`,
`agent.*`, `voice.*`) are written as redacted JSON Lines; history and audit live in SQLite;
per-execution logs are plain text; traces record spans. Details:
[OBSERVABILITY.md](OBSERVABILITY.md).

## 18. Failure handling

| Failure | Behaviour |
|---|---|
| Invalid input / unknown action | Rejected before anything runs; the graph or workflow does not start |
| Approval denied / non-interactive | The action is `denied`; nothing ran; graphs stop at that step |
| Blocked by policy | `blocked`; never runs, not even with `--yes` |
| Command failure | Recorded with exit code and output; graph stops unless `continue_on_error`; rollback if requested |
| Transient failure of a read | Retried with backoff (idempotent actions only) |
| Timeout / Ctrl+C | The action is cancelled and its processes terminated; the session continues |
| A bug in a command | Reported as an unexpected error; the session continues |
| Platform unreachable | Local capabilities remain; the agent reattaches when the platform answers again |
| Model provider failure | Bounded retries, circuit breaker; the conversation is kept |

## 19. Testing strategy

Unit tests for each subsystem (policy, catalog contract, resolver tables, executor semantics,
graphs, files, git against real repositories, workflows, events, voice with fake engines,
plugins), integration tests through the CLI, pseudo-terminal end-to-end tests of the real
binary, boundary tests with tripwires on every AI entry point, an instrumented interpreter
that records environment lookups, a static scan for provider keys and SDK imports, platform
tests against a real server, and a fresh-wheel installation test. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## 20. Production deployment architecture

The CLI is a pure-Python wheel (`pip install highhxcli`) with three runtime dependencies
(click, rich, PyYAML). State is local: `.highhx/state` (SQLite, WAL) per project, a user data
directory for events and global history. The platform (`server/`, FastAPI + SQLAlchemy,
PostgreSQL or SQLite) provides accounts, plans, billing, the streaming AI gateway (resumable
SSE with idempotency keys, per-account concurrency limits, metering) and session sync; it is
stateless across instances and runs behind any HTTP load balancer. See
[platform.md](platform.md).

## 21. Future extensibility

New capabilities are new actions (a spec and a handler) — they immediately work from plain
language (with a resolver rule), `/run`, workflows, the agent (with a plan feature) and the
event log. New interfaces (voice today; an IDE or a chat surface later) call the same
session. New execution targets (remote runners) implement the handler contract. See
[ROADMAP.md](ROADMAP.md).

## 22. Engineering choices that distinguish HighhX

Stated as properties of the implementation, with the tests that hold them:

* **Deterministic before intelligent.** Everything that can be decided by rules is —
  resolution, risk, approval, execution. The model chooses among actions; it never decides
  what is safe (`tests/unit/actions/test_policy_and_catalog.py`).
* **One executor for humans and AI.** Free commands, workflow steps and agent proposals are
  the same actions with the same checks (`test_agent_and_boundaries.py`).
* **Approval semantics bound to the exact action.** HMAC tickets, a five-level table,
  preview-then-approve for whole plans, no pre-approval for the agent (`test_executor.py`).
* **Rollback as part of the contract.** Compensations are declared per action and used by
  `/undo`, graphs and workflows; resume understands what was rolled back
  (`test_workflow_actions.py`).
* **A clean plan boundary.** Free has no path to a model, a provider key or the agent
  runtime; Pro is entitled by the platform, never by local data (`test_free_pro_boundary.py`).
* **Observable by default.** Every intent, decision and outcome is a structured, redacted
  event (`test_session_and_events.py`).
* **Automation that survives failure.** Workflows resume from the first unfinished step and
  can be cancelled from any process (`test_workflow_actions.py`).
* **Measured, not assumed.** Command dispatch through the full safety pipeline costs about
  3 ms; a 64-step parallel workflow runs at over 400 steps per second on a laptop — which is
  why HighhX stays a single Python runtime (see [ARCHITECTURE.md](ARCHITECTURE.md#runtime-choice)).
