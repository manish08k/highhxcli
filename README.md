# HighhXCli

**HighhX is a developer automation CLI.** Run `highhx` in a project and describe what you
want. Everything HighhX does — a command you typed, a sentence, a workflow step, or a step
the AI agent proposed — is an **action** from one catalog, run by one executor that rates its
risk deterministically, asks when the risk calls for it, executes with timeouts and
cancellation, verifies the result, records it and can undo it where that is possible.

| | |
|---|---|
| **HighhX Free — deterministic developer and computer automation** | Plain language → a **deterministic resolver** (no AI) → a JSON action plan → verified automation of websites, applications, the keyboard, files, git and your project (*open Gmail and search internship*, *play lofi on YouTube*, *switch to Slack*, *open my project and run the tests*). The interactive session and the full developer CLI: 99 actions (files, git, packages, docker, databases, services, browser, deployments, security, workflows, shell), workflows with rollback, resume and cancel, `/plan` + `/approve`, `/undo`, voice with local speech-to-text. No AI, no account. |
| **HighhX Pro — deterministic automation + AI developer agent** | The same session with the AI agent attached: open-ended requests, planning, repository-wide changes, debugging, refactoring, AI computer use — proposed as action graphs that the same executor validates, rates, approves and runs. Autonomous tasks: *"fix the login bug and make sure all tests pass"* runs until HighhX itself has verified the tests. |

Run `highhx` in a terminal and describe what you want. Free and Pro share one interface and
one execution platform: the same tools, safety policy, confirmations, verification and audit
trail. Your plan only decides which capabilities the session has — the HighhX platform grants
and enforces them. The AI only chooses actions; it never gets its own way to run them.

## Documentation

[Product specification](docs/PRODUCT_SPEC.md) (start here) ·
[Architecture](docs/ARCHITECTURE.md) · [Action engine](docs/ACTION_ENGINE.md) ·
[Safety model](docs/SAFETY_MODEL.md) · [Security](docs/SECURITY.md) ·
[Free automation](docs/FREE_AUTOMATION.md) ·
[Free and Pro](docs/FREE_PRO.md) · [Agent runtime](docs/AGENT_RUNTIME.md) ·
[Automation](docs/AUTOMATION.md) · [Voice](docs/VOICE.md) ·
[Observability](docs/OBSERVABILITY.md) · [Plugin system](docs/PLUGIN_SYSTEM.md) ·
[CLI reference](docs/CLI_REFERENCE.md) ([all commands](docs/commands.md)) ·
[Roadmap](docs/ROADMAP.md) · [Contributing](docs/CONTRIBUTING.md)

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

- [Documentation](#documentation)
- [Why HighhX](#why-highhx)
- [Installation](#installation)
- [Quick start](#quick-start)
- [The interactive session](#the-interactive-session)
- [HighhX Free: plain-language automation](#highhx-free-plain-language-automation)
- [HighhX Pro: the AI developer agent](#highhx-pro-the-ai-developer-agent)
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

- **Local-first CLI.** Every Free command works without an account or network.
  History, logs and deployment state live in `.highhx/` on your machine.
- **An AI agent that uses your real tools (Pro).** `highhx agent` works through HighhX's
  own commands and safety system — not a chatbot pasting shell snippets.
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

highhx "show git status"                       # plain language works as a command, too
highhx "open Gmail and search internship"      # deterministic, verified, no AI
highhx do --plan --json "play lofi on YouTube" # the JSON action plan, nothing runs
highhx runs                                    # traces of plain-language runs
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

## The interactive session

`highhx` with no arguments, in a terminal, opens the HighhX session (`highhx agent` opens the
same one). Outside a terminal, and with `--json` or `--quiet`, it prints the help as before,
so scripts and CI are unaffected.

```text
$ highhx

  (the Knight)   HighhX v0.6.2
                 Developer command center
                 ~/code/shop
                 main • clean
                 Free • Local

❯ show git status

◉ highhx git status
✓ Working tree clean
✓ highhx git status (0.1s)

❯ Fix the failing tests

╭─ HighhX Pro capability ─────────────────────────────╮
│  AI code changes require HighhX Pro.                │
│                                                     │
│  Available locally, without AI:                     │
│  1  Run the tests         highhx test               │
│  2  Diagnose the project  highhx diagnose           │
│  3  Show recent logs      highhx logs               │
│                                                     │
│  [1-3] Continue locally    [p] View Pro             │
╰─────────────────────────────────────────────────────╯
```

- **Plain language first.** On Free, a deterministic resolver turns known, fully specified
  requests into actions — *run the tests and then build*, *stop the backend*, *deploy
  staging*, *commit all changes with message "…"*, *open localhost 3000* — using your
  project's services, targets and workflows as entities. It never guesses: open-ended
  requests show what HighhX Pro would do and the local actions that can do part of the job.
  On Pro the AI agent handles every request.
- **Preview, approve, undo.** `/plan <request>` shows each step with its risk; `/approve`
  runs exactly that plan; `/undo` reverts the last file changes; `/retry` re-runs a failed
  request; `/resume` and `/cancel` operate workflow runs.
- **`!command`** runs a shell command as an action (classified, approved by risk,
  audited — `rm -rf /` never runs); **`highhx <command>`** runs any HighhX command in the session.
- **Input.** ↑/↓ history (kept across sessions), line editing, `\` at the end of a line or
  a `"""` block for multi-line input, Ctrl+C interrupts the running request, Ctrl+D exits.
- **Status line.** `Free • Local`, `Free • Connected`, `Pro • Connected`,
  `Pro • Offline (cached)` or `… • Platform unavailable`. Only a live answer from the
  platform attaches the AI agent. When the platform cannot be reached, local capabilities
  keep working and the session re-checks when a request needs the agent; if the gateway
  fails during a Pro request, the session says so and offers the local route for it.
- **Deterministic means predictable.** Free never calls a model or reads a provider API key.
  File names are never treated as websites (`open main.py` is not `https://main.py`), and
  changes are approved according to one risk table ([docs/SAFETY_MODEL.md](docs/SAFETY_MODEL.md)).
- **Plan changes apply in place.** Run `highhx login` (or `/account` after upgrading) inside
  the session and it re-checks your account: the AI agent attaches when the platform grants
  it, and detaches when it no longer does.
- **Slash commands**, grouped in `/help`: run (`/plan` `/approve` `/deny` `/retry` `/run`),
  review (`/status` `/history` `/changes` `/undo` `/doctor`), workflows (`/workflows`
  `/workflow` `/resume` `/cancel`), project (`/init` `/context` `/tools` `/config` `/memory`),
  account & AI (`/login` `/account` `/pro` `/usage`, and the agent's `/model` `/mode`),
  session (`/voice` `/clear` `/help` `/quit`). `/status` shows what is waiting on you and which
  command acts on it. See [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md).
- **First run.** The first session shows a short *Getting started*: `/init`, a first request,
  `/plan` + `/approve`, and `/login` for Pro.
- **Voice.** Type `/voice on` (or run `highhx voice`) and speak. The first time, HighhX
  offers to set up everything local voice needs — whisper.cpp, an audio recorder and a
  checksum-verified model — then remembers it. Speech-to-text runs entirely on your machine:
  free, offline, no account, no Pro. Push-to-talk: Enter on an empty line records, Enter stops; the transcript
  goes through exactly the same resolver, plan, approvals, verification and audit as typed
  text, and spoken requests get a spoken reply. `/voice status`, `/voice setup`,
  `/voice test`, `/voice off` — handled by the session itself, never by the resolver or the
  Pro screen ([docs/VOICE.md](docs/VOICE.md)).

```text
❯ /voice on

🎙 Voice on
Speech-to-text: whisper.cpp (base.en)
Microphone: ready
Voice replies: enabled
Push-to-talk: press Enter on an empty line to talk · /voice off

❯                      (Enter on an empty line)
🎙 Listening...
◉ Transcribing...
✓ "Open YouTube and play Adhento Gani"
```

## HighhX Free: plain-language automation

Free understands short requests about your computer and your project — without an LLM, an AI
API or an account — and carries them out as verified, traced automation:

```text
❯ open YouTube and play Adhento Gani

◉ open YouTube  browser.open · low
✓ open YouTube — open https://www.youtube.com (1.5s)
  ↳ verified — YouTube is open
◉ search YouTube for 'Adhento Gani'  browser.search · low
✓ search YouTube for 'Adhento Gani' (1.1s)
  ↳ verified — results for 'Adhento Gani' are showing
◉ play the first YouTube result  browser.play · low
✓ play the first YouTube result — playing 'Adhento Gaani Vunnapaatuga …' (3.8s)
  ↳ verified — playing Adhento Gaani Vunnapaatuga …
3/3 steps · verified · 6.4s · highhx runs show run_20260928T101500_3f2a
```

- **The deterministic resolver** splits the request into clauses, recognises
  verbs (*open, search, play, switch to, press, scroll, click, type, create, list, run* and
  the developer requests), resolves names against a **target registry** (websites,
  applications, project places — data in YAML, extensible in `targets.yaml`) and produces a
  versioned **JSON action plan**: each step's action, target, parameters, risk
  (safe / controlled / high), executor and verification.
- **Execution** goes step by step through the action executor — the same risk
  classification, approvals and guardrails as everything else — and each step is
  **verified** (page open, results showing, media playing, app in front, file exists, exit
  code). A step that claims success but fails verification is a failure; the run stops
  there and says why.
- **Web** steps run in the HighhX browser (its own Chrome profile, via DevTools). **Desktop**
  steps go through the **automation bridge** to the C#/.NET engine (`engine/dotnet`) when
  installed, else the built-in Python engine; keys are never sent to a terminal.
- **Unknown** names are explained (*I don't know an app or website called 'spotifyy' yet*);
  **open-ended** requests (*find the most important email from last week and draft a
  response*) show what HighhX Pro would do. Nothing is guessed.
- **Every run is traced**: `highhx runs`, `highhx runs show`, `highhx runs stats`.

Details: [docs/FREE_AUTOMATION.md](docs/FREE_AUTOMATION.md).

## HighhX Pro: the AI developer agent

```text
$ highhx

     ▗█▖
     ▐▀▌
    ▗ ▄ ▖
   ▗▚▐█▌▞▖
  ▗▚█▐█▌█▞▖      HighhX v0.6.2
   ▟█▐█▌█▙       Developer command center
  ▐▐█▌█▐█▌▌      ~/code/shop
  █▗▜▌█▐▛▖█      main • clean
  █▐▙▘█▝▟▌█      Pro • Connected
  ▐▐▜▌▄▐▛▌▌
 ▗▝▐▐▌█▐▌▌▘▖
 █▙▝█▌█▐█▘▟█
▜▙▜█▖▘█▝▗█▛▟▛
 ▀█▟▀ █ ▀▙█▀
   ▀▌ █ ▐▀

Project   shop Python, FastAPI
AI        Connected HighhX (managed)

What would you like me to do? (/help for commands)

❯ run all the tests and fix whatever fails

╭─ Plan ───────────────────────────────────────────╮
│  1. Run the test suite                           │
│  2. Investigate the failures                     │
│  3. Fix the root cause                           │
│  4. Re-run tests, lint and type checks           │
╰─ Get the test suite green ───────────────────────╯
Proceed? [Y/n]

✓ Run tests — 3 tests failing (4.1s)
✓ Read src/shop/cart.py — 88 lines

╭─ ⚠ Action requires approval ─────────────────────╮
│  Edit src/shop/cart.py (+2 -1)                   │
╰──────────────────────────────────────────────────╯
  …diff…
Proceed? [y/N/a=always this session] y
✓ Edit src/shop/cart.py — +2 -1
✓ Run tests — Tests passing — 48 passed (pytest) (4.3s)
✓ Run checks — all checks passed

Fixed the discount rounding in `cart.total()` (it rounded before applying tax).
All 48 tests pass; lint and type checks are clean.

4 steps  ·  1 file changed (/undo)  ·  38.2k tokens  ·  41s
```

Ask for what you want in plain language — *explain how this project works*, *find and
fix the bugs*, *add authentication*, *find security issues*, *why is the application
crashing?*, *prepare this project for release*, *deploy this*. The agent inspects the
project, proposes a plan for multi-step work, uses HighhX's capabilities as tools
(code search and edits, tests, checks, fixers, builds, dependencies, git, security scans,
doctor/diagnose/repair, workflows, deploy and rollback), verifies what it did and
summarises it.

- **Same safety system.** Commands run through the HighhX engine: risk classification,
  `policies.yaml`, approvals and history. File edits show a diff and ask (unless you
  choose `--mode auto-edit`); pushes, deploys and rollbacks always ask; production
  deploys need typed confirmation. The agent cannot leave the project, read secret files
  or touch `.git/`. `--mode read-only` investigates without changing anything.
- **Slash commands.** Everything from the [interactive session](#the-interactive-session), plus
  `/plan` `/model` `/mode` `/changes` `/undo`.
- **Sessions.** Saved per project: `highhx agent --continue`, `--resume ID`, `highhx agent sessions`.
- **Scriptable.** `highhx agent "…"` outside a terminal (or with `--json`) handles one request
  and exits — `--yes --mode auto-edit` for unattended runs in CI.
- **Providers.** All Pro AI goes through the HighhX platform (authenticated, plan-checked,
  metered); choose the upstream — Anthropic, OpenAI or Gemini — with `highhx account settings`
  or `/model`. No provider key is needed on your machine.
- **Kill switch.** `highhx agent stop` (or Ctrl+C) cancels the model request, retries, running
  commands and their child processes immediately.
- **Computer use.** The agent observes browsers and apps semantically and chooses among valid
  actions; see [docs/computer-use.md](docs/computer-use.md).

```bash
highhx login                 # browser sign-in (creates your account)
highhx account upgrade       # HighhX Pro checkout
highhx                       # interactive session (also: highhx agent)
highhx agent "why is the application crashing?"
```

Guides: [docs/agent.md](docs/agent.md) · [docs/computer-use.md](docs/computer-use.md) · [docs/platform.md](docs/platform.md).

## Commands

Full reference with every option: [docs/commands.md](docs/commands.md).

| Area | Commands |
|---|---|
| HighhX Pro | `agent` `agent sessions` `agent models` `agent stop` |
| Automation (no AI) | `do` `computer status/open/observe/click/type/select/press/scroll/run` `computer browser start/stop` |
| Account | `login` `logout` `account` `account plans/usage/upgrade/billing/settings` |
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
| Observability | `logs [--follow]` `history [id]` `audit` `report` `trace` |
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

Details: [docs/SECURITY.md](docs/SECURITY.md).

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
- **The agent.** Uses the same engine, policies and approvals as you do, plus path
  confinement, secret-file protection and redaction of everything sent to the model.
  Agent actions have policy names (`agent:write`, `agent:exec`, `agent:deploy:<target>` …)
  so `policies.yaml` can restrict or forbid them.
- **No claims.** `highhx security` reports concrete findings from local checks. An empty
  report does not mean a project is secure, and HighhX never says it is.

Report vulnerabilities in HighhX itself as described in [SECURITY.md](SECURITY.md).

## Development

```bash
pip install -e ".[dev]" -e "./server[dev]"
pytest                 # CLI: unit, integration, security and end-to-end tests
(cd server && pytest)  # platform API + live CLI ↔ platform integration tests
ruff check . && ruff format --check .
mypy                   # strict typing of src/highhx
```

The HighhX Platform backend lives in [`server/`](server/README.md).

Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Development guide:
[docs/development.md](docs/development.md) · Troubleshooting: [docs/troubleshooting.md](docs/troubleshooting.md).

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Please open an issue before large changes.

## License

MIT — see [LICENSE](LICENSE).
