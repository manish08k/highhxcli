# Architecture

HighhX is layered so that every side effect passes through one place where safety
rules are enforced.

```text
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
