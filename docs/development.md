# Development

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## Checks

```bash
pytest                          # everything (≈ 90 s)
pytest tests/unit -q            # fast unit tests
pytest -m e2e                   # end-to-end: runs `python -m highhx` in subprocesses
ruff check . && ruff format --check .
mypy                            # configured in pyproject.toml for src/highhx
coverage run -m pytest && coverage combine && coverage report
```

Real-service tests: `tests/integration/test_mysql_real.py` starts a private, throwaway
`mysqld` when the MySQL server binaries are installed (it never connects to an existing
server) and is skipped otherwise. PostgreSQL, Docker, Kubernetes and Terraform are covered
by contract tests that put recording fake executables on `PATH`
(`tests/integration/test_external_tool_contracts.py`); they verify arguments, environment
and exit-code handling but do not contact real services.

## Platform support

Development and all testing so far have happened on macOS (there is no CI pipeline yet). The code avoids platform assumptions — `pathlib`
everywhere, no bash-isms (shell syntax goes to `/bin/sh -c` on POSIX and `cmd /d /s /c`
on Windows), `.bat`/`.cmd` wrappers resolved through `PATHEXT`, process trees stopped with
process groups on POSIX and `taskkill /T` on Windows, POSIX permission checks skipped on
Windows — but Linux and Windows have not yet been exercised by the test suite. Tests that
depend on POSIX signals, permissions or shebang scripts are skipped on Windows.

## Layout

```text
src/highhx/
  cli.py            entry point, global options, sectioned help, error → exit code mapping
  commands/         one module per command; App (composition root) in commands/__init__.py
  core/             engine, executor, context, results, errors, events, lifecycle
  execution/        processes, shells, env, timeouts, cancellation, retries, parallelism, isolation
  workflows/        schema, parser, loader, validator, graph, conditions, variables, scheduler, engine, templates
  …                 domain packages (see docs/architecture.md)
templates/          project templates used by `highhx init` (packaged as highhx/_templates)
schemas/            JSON Schemas generated from the code (tests keep them in sync)
examples/           example .highhx/ setups (validated by tests)
tests/              unit, integration, e2e, security, fixtures
```

## Adding a command

1. Put domain logic in the matching package (e.g. `highhx/deployment/manager.py`),
   running side effects through `engine.run()` / `engine.approve()` with an honest `RiskLevel`.
2. Add a thin click command in `highhx/commands/<area>/<name>.py` that calls it and renders
   with `app.output.emit(data, render)` so `--json` works.
3. Register it in `highhx/commands/registry.py` (top level) or on its group, and add it to
   a section in `cli.py`.
4. Write tests (`tests/unit/commands` for CLI behaviour, domain tests next to the area).
5. Regenerate `docs/commands.md` with `python scripts/generate_docs.py`; a test fails if a
   command is undocumented or lacks help text. If you changed a schema, run
   `python scripts/generate_schemas.py` (a test fails when `schemas/` drifts).

## Adding a project type

Detection lives in `highhx/detection/` (evidence from real files) and suggested commands
in `highhx/project/detector.py`. Add a template directory under `templates/<stack>/`
(`config.yaml` plus optional `workflows/`) using `{{ placeholders }}`; a whole-value
placeholder that resolves to nothing removes the line, and workflow steps without a
command are pruned.

## Releasing HighhX

HighhX releases itself: `highhx release` (Conventional Commits → version → CHANGELOG →
tag), then `python -m build` and `highhx publish`.
