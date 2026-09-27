# Contributing (engineering guide)

Project conventions and the contribution process: [../CONTRIBUTING.md](../CONTRIBUTING.md).
Environment setup: [development.md](development.md). This page is the engineering checklist.

## Setup and checks

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check src tests server && .venv/bin/ruff format --check src tests server
.venv/bin/mypy                               # strict, whole package
.venv/bin/python -m pytest -q                # CLI (unit, integration, e2e, security)
(cd server && ../.venv/bin/python -m pytest -q)   # platform
python scripts/generate_docs.py              # after changing commands or help text
python scripts/generate_schemas.py           # after changing config/workflow/plugin schemas
```

## Rules that keep HighhX what it is

1. **Every side effect is an action or goes through `Engine.run`.** No new subprocess
   calls, no new file writes outside the confinement helpers.
2. **Risk is decided by code.** New actions get an honest risk floor and the right `kind`;
   never let a model, a plugin or a configuration lower a risk.
3. **Retries only for idempotent actions.** If running it twice could change more than once,
   it is not idempotent.
4. **Declare verification and compensation when they are possible.**
5. **Free stays deterministic.** No imports of model SDKs, no reading of provider keys, no
   agent runtime on a Free path. The boundary tests will fail if you try.
6. **No secrets in records.** Events, history, audit and logs go through the redactor;
   events carry names and statuses, not contents.
7. **Commands stay thin.** Parse → call a service or the executor → render. Business logic
   lives in services and actions.

## Where tests go

| Change | Tests |
|---|---|
| Action, policy, resolver, executor | `tests/unit/actions/` (contract tests cover new specs automatically) |
| Workflow engine | `tests/unit/workflows/`, `tests/unit/actions/test_workflow_actions.py` |
| Session / UI | `tests/unit/agent/test_interactive_shell.py`, `tests/unit/actions/test_session_and_events.py` |
| Free/Pro boundary | `tests/unit/agent/test_free_pro_boundary.py` — never weaken these |
| Agent | `tests/unit/agent/` (scripted provider, recording UI) |
| Real terminal | `tests/e2e/` (pseudo-terminal) |
| Platform | `server/tests/` |

Tests use real files, real git repositories and real subprocesses where possible; fakes are
limited to the model provider, the platform client and audio devices.
