# The action engine

Every capability HighhX executes is an **action**: a named, typed, risk-rated operation
with a deterministic handler. Plain-language requests, `/run`, `highhx actions run`,
workflow `action:` steps and the Pro agent's `run_actions` graphs all execute actions
through one `ActionExecutor`. Code: `src/highhx/actions/`.

## The contract (`ActionSpec`)

| Field | Meaning |
|---|---|
| `name` | `category.verb`, e.g. `git.push` (aliases allowed: `browser.navigate`) |
| `description` | One line, shown in previews, `/tools` and to the agent |
| `inputs` | Input schema (validated before anything runs; exported as JSON Schema) |
| `outputs` | Output fields and their meaning |
| `risk` | The **floor**: the effective risk is at least this (see [SAFETY_MODEL.md](SAFETY_MODEL.md)) |
| `kind` | How the safety classifier sees the action (`exec`, `git`, `write_file`, `deploy`, `ui_click` …) |
| `permissions` | What it touches: `project.read`, `project.write`, `process.run`, `git.local`, `git.remote`, `network`, `containers`, `database`, `deploy`, `browser`, `desktop` |
| `timeout` | Seconds before the action is cancelled |
| `retry`, `idempotent` | Retries apply **only** to idempotent actions, only to transient failures, with exponential backoff |
| `verify` | How success is checked (read-back digest, absence, new HEAD …); a failed check fails the action |
| `compensate` | How a completed action is undone (rollback) — only where that is technically possible |
| `feature` | The plan feature the AI agent needs to propose it (Free users run everything directly) |
| `agent` | Whether the agent may propose it through `run_actions` (UI actions use the computer-use tools) |
| `command`, `target`, `environment`, `policy_action` | What the classifier, the preview and `policies.yaml` see |

`highhx actions list [category]`, `highhx actions show NAME` and `/tools` print the catalog;
`highhx actions plan NAME key=value …` rates an action without running it.

## The catalog (85 actions)

| Category | Actions |
|---|---|
| project | detect, init, status, check, test, fix, build, run (dev), start, stop |
| filesystem | read, write, copy, move, delete, search |
| git | status, diff, log, branch, checkout, commit, tag, push, pull, revert |
| package | install, update, audit, outdated |
| docker | build, run, stop, logs |
| database | connect, migrate, backup, restore |
| service | start, stop, restart, logs |
| browser | open (navigate), search, play, find, click, double_click, hover, drag, fill (type), press, upload, download, wait, extract (read), screenshot, back, forward, refresh (reload), new_tab, close_tab, switch_tab, tabs |
| computer | launch |
| deployment | deploy, rollback, status, logs |
| security | scan, doctor, diagnose |
| workflow | run, resume, cancel |
| shell | run |

Plugins add `plugin.<plugin>.<command>` actions per project ([PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md)).

### How handlers work

* **Command-backed** (`handlers/delegate.py`): the action runs the existing HighhX command
  in-process (`highhx git status`, `highhx deploy to staging` …) — the same code path as
  typing it, including the engine's own policy and risk checks. Output: the command and its
  exit code.
* **Native** (`handlers/files.py`, `handlers/native.py`): file operations with confinement
  and journaling; git operations through the engine; browser actions through the
  deterministic flow runner (element resolution, per-element safety check, re-observation);
  project detection. Output: structured data.

## From plain language to actions

Free's plain-language requests reach the executor as a **JSON action plan** built by the deterministic resolver
([FREE_AUTOMATION.md](FREE_AUTOMATION.md)): each plan step names a catalog action and its
parameters, and the plan runner executes the steps in order through `plan()` / `execute()`
below — so the plan's risk is re-classified and approved exactly like any other action —
then applies the step's verification strategy and stops at the first failure. Desktop UI
actions (`computer.*`) perform their operations through the automation bridge
(`ActionExecutor.automation()`), never by calling OS tools directly.

## Execution pipeline

```text
plan(name, inputs)
  unknown action → UnknownActionError       invalid inputs → ValidationError (nothing runs)
  descriptor = kind, summary, target, command, environment, actor
  verdict   = SafetyPolicy.classify(descriptor)             (deterministic)
  decision  = decide(spec.risk, verdict, command)           (five levels + approval rule)

execute(planned)
  action.planned event          --dry-run → status "planned", nothing runs
  gate.authorize(...)           project policy · read-only mode · approval by the table
                                · HMAC ticket bound to this exact action · audit
      blocked → status "blocked"      denied → status "denied"
  engine.operation("action")    history entry (commands inside become its steps)
  gate.executing(ticket)        ticket redeemed against the action as it runs
  handler, with a timeout timer and a child cancellation token
      idempotent + transient failure → action.retry, backoff, again
  verify                        failure → status "failed", verified=false
  action.completed | action.failed
```

`ActionResult`: `ok`, `status` (ok · failed · denied · blocked · cancelled · timeout ·
planned), `summary`, `output`, `changed` (paths), `verified`, `error`, `attempts`,
`seconds`, `compensated`.

## Action graphs

`run_graph(executor, nodes, rollback=…)` executes `ActionNode(id, action, inputs,
depends_on, continue_on_error)` lists:

1. **Validate the whole graph first** — unique ids, known dependencies, no cycles, every
   action known and every input valid. Nothing runs if anything is wrong.
2. Run in deterministic topological order; a failing node stops the graph unless it allows
   failure; nodes whose dependencies failed are skipped.
3. With `rollback`, completed nodes are compensated newest-first.

The Pro agent's `run_actions` tool is a graph; so is a compound plain-language request.

## Compensation (rollback)

| Action | Undo |
|---|---|
| filesystem.write / copy / delete (file) | restore the journaled original (or remove a created file) |
| filesystem.move | move back |
| git.commit | `git reset --soft HEAD~1` — only if HEAD is still that commit (changes kept) |
| git.tag | delete the tag |
| git.checkout | switch back to the previous branch |
| git.pull | `git reset --keep` to the previous HEAD |

Not undoable: pushes, deployments (roll back with `deployment.rollback`), database changes
(restore from a backup), deleted directories, anything outside the machine.

## Adding an action

1. Write a handler `(ctx: ActionContext, inputs) -> ActionResult` — or reuse `delegate(argv)`
   when a HighhX command already does the job.
2. Register an `ActionSpec` in `catalog.py`: honest risk floor, the right `kind` (so the
   classifier sees it), permissions, a timeout, `idempotent=True` only if running it twice is
   harmless, `verify` and `compensate` where possible, and the agent `feature`.
3. Optionally add a resolver rule (`resolver.py`) so plain language reaches it.
4. Tests: the catalog contract tests cover the spec automatically
   (`tests/unit/actions/test_policy_and_catalog.py`); add behaviour tests for the handler.
