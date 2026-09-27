# Automation

Workflows are the automation primitive of HighhX: YAML graphs in `.highhx/workflows/`
whose steps are shell commands, HighhX actions or other workflows. They are deterministic,
need no AI and run the same on Free and Pro. Syntax basics (inputs, variables, expressions,
outputs): [workflows.md](workflows.md).

## A complete example

```yaml
name: deploy
description: test, scan, build, approve, deploy, verify — roll back if anything fails
on_failure: rollback                   # undo completed steps (newest first) when a step fails
inputs:
  environment: {default: staging}
steps:
  - id: test
    action: project.test
  - id: scan
    action: security.scan
    depends_on: test
  - id: build
    action: docker.build
    depends_on: scan
  - id: deploy
    action: deployment.deploy
    with: {environment: "${{ inputs.environment }}"}
    depends_on: build
    approval: {message: "Deploy to ${{ inputs.environment }}?", risk: dangerous}
    rollback: {action: deployment.rollback, with: {environment: "${{ inputs.environment }}"}}
  - id: health
    run: curl -fsS https://${{ inputs.environment }}.example.com/health
    depends_on: deploy
    retry: {attempts: 5, delay: 5s, backoff: 2}
    timeout: 2m
```

`highhx workflow run deploy -i environment=staging` — if the health check still fails after
its retries, the deployment step is rolled back with `deployment.rollback`.

## Steps

| Key | Meaning |
|---|---|
| `run` | Command(s); risk-classified after variable substitution, approval by risk |
| `action` + `with` | A catalog action ([ACTION_ENGINE.md](ACTION_ENGINE.md)); inputs may use `${{ }}`; outputs become `steps.<id>.outputs.*` |
| `uses` + `with` | Another workflow (inputs), nested up to 8 levels |
| `depends_on`, `if` | Ordering and conditions (`success()`, `failure()`, `always()`, expressions) |
| `retry`, `timeout` | Attempts with exponential backoff; per-step timeout |
| `approval` | An explicit approval before the step (message, risk, bypassable) |
| `continue_on_error` | The step may fail without failing the workflow |
| `rollback` | How to undo the step: `true` (the action's own compensation), a command or list of commands, or `{action, with}` |

Workflow keys: `on_failure: stop | rollback`, `settings` (`fail_fast`, `max_parallel`,
`timeout`, `working_directory`), `env`, `vars`, `inputs`, `outputs`, `on` (event triggers).

Every step is classified and approved as it runs — an `action:` step through the action
executor, a `run:` step through the engine. `highhx workflow validate` checks everything
statically: unknown actions, invalid action inputs, rollbacks that cannot work
(`rollback: true` on a push), dependency cycles, expressions, missing tools.

## Running and operating workflows

| Command | Purpose |
|---|---|
| `highhx workflow run NAME [-i k=v] [-e K=V]` | Run (same as `highhx run NAME`) |
| `highhx workflow inspect NAME` | Stages, kinds, dependencies, conditions, approvals, retries, rollback |
| `highhx workflow runs [--running]` | Execution history with status |
| `highhx workflow inspect RUN_ID` | One run's steps: status, exit code, duration, error |
| `highhx workflow resume RUN_ID` | Run again with the same inputs, reusing every step that succeeded (and its outputs); steps whose effect was rolled back run again |
| `highhx workflow cancel RUN_ID` | Interrupt the process running it (any terminal); recorded as cancelled, resumable |
| `highhx workflow graph NAME`, `validate`, `create`, `list` | Structure, checks, templates |

In the session: `/workflows`, `/workflow run|inspect|runs|resume|cancel …`, `/resume` (the
last failed run), `/cancel`.

## Rollback semantics

* Only steps that **completed** are undone, newest first, and only when the workflow failed
  (not when it was cancelled — cancel, inspect and resume instead).
* Each undo is classified and approved like any other action; a failing undo is reported
  and the rollback continues with the next step. The result lists every undo
  (`rollback: [{step, ok, detail}]`).
* Resume skips steps that succeeded **and were not rolled back**.

## Starting workflows

| Mechanism | Configuration | Runs via |
|---|---|---|
| On demand | — | `highhx workflow run`, the session, the agent (`workflow.run`) |
| Schedules | `schedules:` in config (cron syntax) | `highhx schedule run` (foreground scheduler), `highhx schedule trigger NAME` |
| Events | `on:` in the workflow, `triggers:` in config | `highhx trigger EVENT` (from CI, git hooks, other tools) |
| Git hooks | `hooks:` in config | `highhx hook install` |
| File changes | `watch:` in config | `highhx watch` |

All of them go through the same engine, approvals (non-interactive runs deny what needs a
person unless `--yes` covers it) and history.

## Execution guarantees

* A step never starts before everything in its `depends_on` succeeded; independent steps run
  in parallel up to `max_parallel`.
* Cancellation (Ctrl+C, `workflow cancel`) stops running commands (process groups) and records
  the run as cancelled.
* Every run is recorded (`highhx history`, `workflow runs`) with its inputs, steps, outputs
  and rollback, and emits `workflow.*` and `step.*` events.
