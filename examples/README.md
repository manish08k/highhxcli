# HighhX examples

Each directory contains a `.highhx/` folder you can copy into a project of the
same kind. Every example is validated by the test suite
(`tests/unit/test_schemas_examples.py`), so they always match the current schema.

| Example | Shows |
|---|---|
| [basic](basic) | The smallest useful setup: commands, one workflow, a task |
| [python](python) | uv-based Python project: parallel lint/typecheck/test, coverage, release |
| [node](node) | pnpm + Next.js: scripts, services, conditional steps |
| [flutter](flutter) | Flutter analyze/test/build with platform conditions |
| [docker](docker) | Compose services, image builds, Docker deployment target |
| [monorepo](monorepo) | Workspace members, running a command across packages |
| [ci](ci) | A CI pipeline: retries, timeouts, outputs, reusable workflows, `on:` triggers |
| [production](production) | Staging + production targets, health checks, rollback, strict policies, SQL migrations |

Run any example's workflow with `highhx run <name>` after copying `.highhx/`
into your project (and adjusting commands to your tooling).
