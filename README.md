# HighhX

**HighhX is a local-first developer command center.** One CLI — `highhx` — detects
your project, runs your workflows, manages environments, dependencies, tests,
builds, git, releases, deployments, services and databases, and keeps a searchable
history of everything it did. Every risky action is classified and needs approval.

```text
$ highhx doctor

HighhX Doctor

✓ macOS 15.2 arm64
✓ HighhX on Python 3.13.1
✓ git 2.47.1
✓ node 22.12.0
✓ python satisfies >=3.11 — declared in pyproject.toml
✓ Configuration valid
✓ Workflows valid — 7 workflow(s)
⚠ docker unavailable — required by this project
✗ Missing DATABASE_URL — profile development

Suggested actions
  → Install docker and make sure it is on PATH.
  → Set it with `highhx env set DATABASE_URL --profile development` or export it in your shell.
```

- [Why HighhX](#why-highhx)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Workflows](#workflows)
- [Configuration](#configuration)
- [Plugins](#plugins)
- [Security model](#security-model)
- [Development](#development)
- [Contributing](#contributing)

## Why HighhX

Most projects accumulate a pile of scripts, Makefile targets, README snippets and
CI YAML that only half the team remembers. HighhX gives every project the same
front door:

- **Local-first and offline-first.** No cloud backend, no account, no API keys, no AI.
  Everything — history, logs, deployment state — lives in `.highhx/` on your machine.
- **Detects instead of asking.** Python (pip/uv/poetry/pdm/pipenv), Node (npm/pnpm/yarn/bun),
  React, Next.js, Flutter/Dart, Java (Maven/Gradle), C/C++ (CMake/Make), Go, Rust, Docker,
  common databases and monorepos — from real manifests and lockfiles, not file extensions.
- **Safe by default.** Commands are classified as safe, normal, dangerous or critical.
  `git push`, `rm -rf`, `DROP TABLE`, production deploys and restores need approval;
  `--dry-run` previews anything; policies can make approvals non-bypassable even with `--yes`.
- **A real workflow engine.** YAML workflows with dependency graphs, parallel execution,
  conditions, variables, retries with backoff, timeouts, approvals and reusable workflows —
  validated before anything runs.
- **Scriptable.** Every command supports `--json` and meaningful exit codes.

## Installation

HighhX needs Python 3.11+. It is developed and tested on macOS; Linux and Windows
support is designed in (see [docs/development.md](docs/development.md#platform-support))
but not yet verified on those systems.

> HighhXcli is **not yet published on PyPI**. Install it from a built wheel or from source.

```bash
# from a checkout of this repository
python -m pip install build && python -m build          # creates dist/highhxcli-*.whl
pipx install dist/highhxcli-*.whl                       # or: pip install dist/highhxcli-*.whl

# development install
python -m venv .venv && . .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Both `highhx` and `highhxcli` are installed as commands, and `python -m highhx` works too.

Runtime dependencies are deliberately small: `click`, `rich` and `PyYAML`.
External tools (git, docker, kubectl, psql …) are used only when a feature needs them.

## Quick start

```bash
cd your-project
highhx init            # detect the project, create .highhx/ (never overwrites without --force)
highhx doctor          # check tools, config, env vars, ports
highhx status          # dashboard: git, environment, services, recent runs
highhx test            # auto-detected test runner (pytest, vitest, jest, flutter, mvn, …)
highhx run ci          # run a workflow; parallel where dependencies allow
highhx run ci --dry-run
highhx history         # what ran, when, how long, exit codes
highhx logs            # output of the last execution (secrets redacted)
```

`highhx init` creates:

```text
.highhx/
├── config.yaml          # commands, services, deploy targets, approvals …
├── environment.yaml     # declared variables and profiles (values live in .env files)
├── policies.yaml        # protected branches, forbidden files, rules
├── workflows/           # dev, test, build, ci, release, deploy, rollback
├── hooks/               # scripts for git hooks
├── state/               # local database (git-ignored)
└── logs/                # execution logs (git-ignored)
```

## Commands

Full reference with every option: [docs/commands.md](docs/commands.md).

| Area | Commands |
|---|---|
| Project | `init` `status` `info` `dev` `start` `stop` `restart` `check` |
| Code & tasks | `run <workflow>` `exec <command>` `script <name>` `task <name>` `watch` `fix` |
| Dependencies | `deps` `deps install` `deps update` `deps outdated` `deps audit` `deps clean` |
| Testing & build | `test [--watch] [--coverage] [--changed]` `benchmark` `build` `clean` `package` `artifacts` |
| Environment | `env` `env check` `env set` `env profile` `env diff` |
| Git & releases | `git status/diff/branch/commit/sync/tag/history` `version` `changelog` `release` `publish` |
| Deployment | `deploy [target]` `deploy status` `deploy logs` `rollback` `environments` |
| Security | `security` `security scan/secrets/deps/config/report` |
| Containers & data | `docker up/down/logs` `services` `ports` `db status/migrate/seed/backup/restore` |
| Workflows & automation | `workflow list/validate/create/graph` `schedule` `hook` `trigger` `watchers` |
| Observability | `logs [--follow]` `history [id]` `report` `trace` |
| Extensibility & team | `plugin list/install/remove/update/search/trust` `config` `policy` `workspace` `profile` |
| Diagnostics | `doctor` `diagnose` `repair` `debug` |

Global options (work before or after the command): `--json`, `--dry-run`, `--yes/-y`,
`--force`, `--quiet/-q`, `--verbose/-v`, `--debug`, `--no-color`, `--cwd/-C DIR`,
`--config-profile NAME`, `--version`, `--help`.

A few examples:

```bash
highhx exec -- pytest -q                 # run anything with env profile, risk check, history
highhx exec --timeout 30s --retry 3 -- ./flaky.sh
highhx test --changed                    # only tests related to files changed since HEAD
highhx deps update                       # shows what will change, then asks
highhx env set DATABASE_URL              # prompts without echo; value never printed again
highhx release                           # version from Conventional Commits, changelog, tag
highhx deploy staging --version 1.4.0    # preflight → approval → deploy → health check
highhx rollback staging                  # restore the previous successful deployment
highhx security --fail-on high           # exit 9 if high/critical findings
highhx workflow graph ci --format mermaid
highhx status --json | jq .git.branch
```

## Workflows

Workflows are YAML files in `.highhx/workflows/`. Full reference:
[docs/workflows.md](docs/workflows.md) · JSON Schema: [schemas/workflow.schema.json](schemas/workflow.schema.json).

```yaml
name: production

settings:
  fail_fast: true
  max_parallel: 4
  timeout: 30m

steps:
  - id: test
    run: pytest

  - id: lint
    run: ruff check .

  - id: build
    run: docker build -t myapp .
    depends_on: [test, lint]          # test and lint run in parallel first

  - id: deploy
    run: ./deploy.sh ${{ steps.build.outputs.tag }}
    depends_on: [build]
    approval: true                    # asks before running
    retry:
      attempts: 3
      delay: 10s                      # exponential backoff: 10s, 20s …

  - id: notify
    run: ./notify.sh "deploy failed"
    depends_on: [deploy]
    if: failure()
```

A step never starts before every step in its `depends_on` succeeded. Steps whose
dependencies are satisfied run in parallel (up to `max_parallel`). Also supported:
`if:` conditions, `${{ }}` variables (`env`, `vars`, `inputs`, `steps.<id>.outputs`),
step outputs, `continue_on_error`, per-step `timeout`/`cwd`/`env`, reusable workflows
(`uses: other-workflow` with `with:` inputs), and `on:` event triggers.

`highhx workflow validate` catches circular and missing dependencies, duplicate ids,
unknown fields, bad expressions, references to steps that are not dependencies,
unparsable commands, impossible steps and risky commands without `approval` —
before anything runs.

## Configuration

`.highhx/config.yaml` is validated strictly (unknown keys are errors with
"did you mean" suggestions). Reference: [docs/configuration.md](docs/configuration.md) ·
JSON Schema: [schemas/config.schema.json](schemas/config.schema.json).

```yaml
version: 1
project:
  name: shop
commands:              # override anything HighhX detected
  test: uv run pytest
  dev: uv run uvicorn app.main:app --reload
services:              # highhx start / stop / services
  api:
    command: uv run uvicorn app.main:app --port 8000
    port: 8000
    health: {url: http://127.0.0.1:8000/health}
deploy:
  default: staging
  targets:
    staging:
      type: ssh                       # local | docker | ssh | kubernetes | terraform | plugin:<name>
      host: deploy@staging.example.com
      command: ./deploy.sh {{ version }}
      rollback_command: ./deploy.sh {{ previous_version }}
      health_check: {url: https://staging.example.com/health}
approvals:
  auto_approve: normal                # never prompt at or below this risk
  yes_max_risk: critical              # the highest risk --yes may approve
```

Config profiles (`.highhx/profiles/ci.yaml`) overlay the config with `--config-profile ci`.
Environment profiles (development/staging/production) are separate and live in
`.highhx/environment.yaml` plus your `.env` files — see `highhx env --help`.

## Plugins

Plugins add commands, workflows, templates, detectors and deployment backends.
Guide: [docs/plugins.md](docs/plugins.md).

```yaml
# my-plugin/highhx-plugin.yaml
name: greet
version: 1.0.0
api_version: 1
permissions: [commands]
contributes:
  commands:
    - name: greet
      run: echo "hello"
  workflows: [workflows]
```

```bash
highhx plugin install ./my-plugin      # or a git URL, or a name from plugins.index
highhx greet
```

Declarative contributions run as subprocesses with an isolated environment.
Python code plugins run only when `plugins.allow_code: true` **and** you trusted those
exact files (by SHA-256) with `highhx plugin install` or `highhx plugin trust`. Trust is
stored in your user data directory, so a cloned repository cannot enable its own plugin code.

## Security model

Details: [docs/security.md](docs/security.md).

- **Approvals.** Every command is classified (safe / normal / dangerous / critical).
  Anything above `approvals.auto_approve` asks; critical actions require typing a word.
  Without a terminal, HighhX denies instead of guessing. `--yes` approves up to
  `approvals.yes_max_risk`, never beyond, and never for non-bypassable rules
  (e.g. `rm -rf /`, `terraform destroy`, policy rules with `bypassable: false`).
- **Policies.** `.highhx/policies.yaml` can deny commands/actions, require approval,
  protect branches, forbid committed files and require a clean tree for releases/deploys.
- **Secrets.** Secret values are never printed: `env` masks them, logs and history are
  redacted (known secret values plus token patterns), and `security secrets` reports
  file/line/type only. New `.env` files get owner-only permissions and are git-ignored.
- **No claims.** `highhx security` reports concrete findings from local checks. An empty
  report does not mean a project is secure, and HighhX never says it is.

Report vulnerabilities in HighhX itself as described in [SECURITY.md](SECURITY.md).

## Development

```bash
pip install -e ".[dev]"
pytest                 # unit, integration, security and end-to-end tests
ruff check . && ruff format --check .
mypy                   # strict typing of src/highhx
```

Architecture: [docs/architecture.md](docs/architecture.md) · Development guide:
[docs/development.md](docs/development.md) · Troubleshooting: [docs/troubleshooting.md](docs/troubleshooting.md).

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Please open an issue before large changes.

## License

MIT — see [LICENSE](LICENSE).
