# Workflows

Workflows live in `.highhx/workflows/<name>.yaml` (`.yml` and `.json` work too; plugins can contribute more).
Run with `highhx run <name>`; preview with `--dry-run`; inspect with
`highhx workflow graph <name>`; check with `highhx workflow validate`.
JSON Schema: [schemas/workflow.schema.json](../schemas/workflow.schema.json).

## Schema

```yaml
name: ci                         # required
description: Continuous integration
on: [push]                       # events for `highhx trigger push`
inputs:                          # highhx run ci --input target=staging
  target: {description: …, default: staging, required: false}
env: {PYTHONUNBUFFERED: "1"}     # for every step (supports ${{ }})
vars: {image: my-app}            # constants: ${{ vars.image }}
settings:
  fail_fast: true                # on failure: cancel running steps, skip pending ones
  max_parallel: 4
  timeout: 30m                   # whole workflow
  working_directory: services/api
steps:                           # required, at least one
  - id: build                    # required, unique: letters, digits, _ and -
    name: Build image            # display name
    run: docker build -t ${{ vars.image }} .     # string or list of commands
    depends_on: [test, lint]     # string or list
    if: env.CI == 'true'         # condition (see below)
    env: {DOCKER_BUILDKIT: "1"}
    cwd: docker/
    timeout: 10m                 # per command
    retry: {attempts: 3, delay: 10s, backoff: 2, max_delay: 2m, retry_on_timeout: true}
    approval: {risk: critical, message: Pushes an image, bypassable: false}   # or `true`
    continue_on_error: false     # true: failure doesn't fail the workflow or dependents
    shell: false                 # force or forbid running through the shell
  - id: reuse
    uses: tests                  # run another workflow as this step
    with: {marker: "not slow"}   # its inputs
outputs:
  image: ${{ steps.build.outputs.image }}
```

YAML notes: duplicate keys are an error (plain YAML silently keeps the last one); an
unquoted `on:` key is read as the trigger list (not the boolean `true`); `if: false` /
`if: true` are accepted as constant conditions.

Durations accept `30`, `30s`, `5m`, `1.5h`, `250ms`. `retry: 3` means three attempts
with a one-second initial delay.

## Execution rules

- A step starts only after **every** step in `depends_on` has finished successfully
  (or failed with `continue_on_error`).
- Independent ready steps run in parallel, at most `max_parallel` at a time.
- If a dependency fails or is skipped, dependents are skipped unless their condition
  uses `always()` or `failure()`.
- With `fail_fast: true` (default) a failure cancels running steps (their processes are
  terminated) and skips pending ones that don't opt in with `always()`/`failure()`.
- Ctrl+C cancels the workflow gracefully; the run is recorded as `cancelled` (exit 130).
- The result is recorded in history with every step; logs show `[step-id]` prefixes.

## Conditions and expressions

`if:` and `${{ … }}` use the same small expression language:

| Syntax | Example |
|---|---|
| Contexts | `env.CI`, `vars.name`, `inputs.target`, `steps.build.outputs.tag`, `steps.test.status`, `steps.test.exit_code`, `workflow.name`, `execution.id`, `project.root`, `platform` (`linux`/`macos`/`windows`) |
| Operators | `==` `!=` `<` `<=` `>` `>=` `&&`/`and` `\|\|`/`or` `!`/`not`, parentheses |
| Literals | `'text'`, `"text"`, `42`, `true`, `false`, `null` |
| Status functions | `success()`, `failure()`, `always()`, `cancelled()` |
| Functions | `contains(a, b)`, `startsWith(a, b)`, `endsWith(a, b)`, `format('{0}-{1}', a, b)`, `toJSON(x)`, `exists('path')` |

String comparison is case-insensitive. A condition without a status function is
implicitly `success() && (…)`.

## Step outputs

A step writes `key=value` lines (or `key<<EOF … EOF` for multi-line values) to the file
named by `$HIGHHX_OUTPUT`:

```yaml
- id: version
  run: python -c "import os; open(os.environ['HIGHHX_OUTPUT'], 'a').write('value=1.4.0\n')"
- id: tag
  run: git tag v${{ steps.version.outputs.value }}
  depends_on: [version]
```

Referencing a step that is not a (transitive) dependency is a validation error,
because its outputs would not exist yet.

## Validation

`highhx workflow validate [names…] [--strict]` reports:

- schema errors (unknown fields, wrong types, missing `run`/`uses`),
- duplicate step ids, missing and circular dependencies, self-dependencies,
- invalid expressions, unknown contexts/inputs/vars/steps,
- step outputs interpolated into a command line (warning — pass them through `env:`),
- references to steps that are not dependencies,
- commands that cannot be parsed, programs not found on PATH (warning),
- dangerous commands without `approval` (warning),
- impossible steps (depending on a step whose condition is always false),
- unknown or recursive reusable workflows and missing/unknown `with:` inputs.

Workflows are validated again before every run; nothing executes if they are invalid.

## Graphs

```bash
highhx workflow graph ci                  # stages (parallel groups)
highhx workflow graph ci --format dot     # Graphviz
highhx workflow graph ci --format mermaid
```
