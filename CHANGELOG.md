# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/) and the
project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-09-27

One interactive HighhX for both plans: run `highhx` and describe what you want.

### Added

- **Interactive session.** `highhx` with no arguments in a terminal opens the HighhX session
  (`highhx agent` opens the same one). Free and Pro share the interface; the plan decides
  which capabilities are attached. Status line: `Free • Local`, `Free • Connected`,
  `Pro • Connected`, `Pro • Offline (cached)`, `… • Platform unavailable`.
- **Capabilities** (`highhx.cloud.capabilities`): local capabilities (commands, shell,
  deterministic intents, deterministic automation) plus the platform-reported plan
  features. The platform stays authoritative and enforces every AI request.
- **Free in the session:** known plain-language requests run the matching HighhX command
  deterministically (the `highhx do` rules, no AI); `!command` runs shell commands through
  the engine (risk, policy, approval, history); `highhx <command>` runs any command in place.
  Requests that need AI show a **HighhX Pro capability** panel naming the capability, with
  local alternatives (`[1-3] Continue locally`, `[p] View Pro`).
- **Platform resilience:** without the platform, local capabilities keep working; when the
  AI gateway fails during a request, the session says so and offers the local route.
- **Plan changes apply in place:** `highhx login` / `/account` in the session re-checks the
  account and attaches or detaches the AI agent.
- Slash commands `/tools`, `/config`, `/account`, `/pro`; agent-only commands explain
  that they need Pro on Free.
- Input: `"""` blocks for multi-line input, readline-safe coloured prompt (clean redraw of
  long lines and history), Ctrl+C at the prompt no longer cancels the application token.
- Tool outcomes distinguish blocked / declined (⊘) and cancelled (○) from failures (✗);
  approvals render as panels.

### Security

- The AI agent runtime is only constructed on a live confirmation from the platform:
  `create_session` refuses a cached account, and a cached account grants no platform
  capability (the cache is an editable local file). Previously an edited cache could start
  the runtime offline (it still could not reach a model — the gateway checks every request).
- Free/Pro boundary tests: tripwires on every AI entry point, a static scan for
  bring-your-own-key paths and vendor SDK imports, and an instrumented interpreter that
  records environment lookups (no provider key is ever read on Free).

### Fixed

- `highhx do` and the session no longer treat file names as websites or applications
  (`open main.py` opened `https://main.py`).
- `highhx -v agent …`, `highhx -C DIR agent …` or a bare `highhx -v` typed inside the
  session no longer start a nested session.
- An unexpected error in one command or agent turn no longer ends the session.
- Known requests that change files (`fix`, `install dependencies`) are confirmed first when
  typed as free-form text in the session.
- A signed-in session that started offline attaches the AI agent once the platform is back.

### Changed

- The startup banner is the same for Free and Pro ("Developer command center" with git
  state and plan status).
- `highhx` outside a terminal, and with `--json` or `--quiet`, still prints the help (CI and
  scripts are unaffected). One-shot `highhx agent "…"` requests still require Pro (exit 10).

## [0.3.0] - 2026-09-27

Two plans, one platform: **Free** is deterministic HighhX; **Pro** adds the AI agent and AI
computer use on top of the same tools, safety, confirmation, verification and audit.

### Terminal identity

- **The HighhX knight**: terminal-native brand art (Unicode quadrant blocks, ASCII fallback;
  full 26x31 and compact 13x15) shown by `highhx` on a terminal and at the start of
  `highhx agent`, sized to the terminal (`HIGHHX_BANNER=full|compact|off`, `HIGHHX_ASCII`).

### Reliability and security hardening

- Platform: AI stream state and rate limits live in the database (migration 0003), so any
  instance can resume, replay or cancel a stream; dead instances' streams end with a retryable
  error; tested with two instances sharing only the database (SQLite and PostgreSQL).
- Providers: cancellation aborts the upstream socket immediately; inactivity timeouts; SDK
  retries only before streaming; truncated streams are errors, not answers; raw transport errors
  are normalised — tested through the real Anthropic, OpenAI and Gemini SDKs over HTTP.
- Browser: a lost DevTools answer is an *unknown* outcome that is never repeated (also after a
  reconnect); per-call timeouts now fire when the browser is silent; waits for navigations
  started by an action; page-text changes count for verification; sockets closed on crashes.
- OCR is available as a read-only `screen` source; OCR and accessibility subprocesses (and
  their children) are killed on cancellation.
- Migration names are validated before use in SQL (MySQL backslash escaping); project XML
  manifests with DTDs/entities are refused; health checks only speak http(s).

### Features

- **Shared safety layer** (`highhx/safety`) for Free automation and the agent: semantic
  classification of commands (parsed pipelines, `sh -c`, `sudo`, force pushes, destructive SQL,
  installs, publishing, credentials, permissions, security controls, remote code, production)
  and UI actions (role/type/structure and multilingual labels); explicit confirmation panel;
  single-use HMAC approval tickets bound to the exact action; blocked catastrophic actions;
  redacted audit log (`highhx audit`).
- **Computer use** (`highhx/computer`): semantic observations, finite action candidates,
  observe → act → re-observe → verify runtime with recovery; Chrome/Chromium/Edge/Brave via
  DevTools; macOS Accessibility; OCR via tesseract. Free: `highhx computer …` commands and YAML
  flows; Pro: `computer_observe`, `computer_act`, `browser_open`, `app_open` agent tools.
- `highhx do "<request>"`: deterministic plain-language requests without AI.
- `highhx agent stop`: kill switch that cancels model requests, retries, tools and their child
  processes.
- Model calls: exponential backoff with jitter, bounded attempts, circuit breaker, per-attempt
  idempotency keys; tools are never retried automatically.
- Resumable streaming: SSE event ids, `Last-Event-ID` resume without re-calling the model,
  duplicate suppression, remote cancellation.
- CLI ↔ platform protocol versioning (`X-HighhX-Protocol: 1.1`, 426 on incompatibility).
- Agent session state machine (`created`, `running`, `waiting_for_confirmation`, `completed`,
  `failed`, `cancelled`, `closed`) enforced on CLI and platform; single-process session leases.
- Tool results carry structured `error_code` and `verified`; per-tool timeouts.
- Prompt-injection defence: tool output and external content framed as untrusted data.

### Changed

- All Pro AI goes through the HighhX platform (authenticated, entitled, metered). The
  client-side bring-your-own-key path was removed; `--provider` selects the gateway upstream.
- Platform 0.2.0: Alembic migrations (upgrades 0.1.0 databases in place), token expiry and
  rotation, account suspension, subscription expiry with grace, out-of-order and concurrent
  webhook handling, `invoice.paid` renewals, idempotent metering, per-account concurrency limit,
  computer-use entitlement check, upstream fallbacks, `/readyz`, optional CORS.

### Fixed

- Stripe: the webhook handler fetched subscriptions with a POST (which updates them); it now
  uses GET. Subscription events now record the Stripe customer, so later invoice events apply.
- Concurrent HighhX processes opening a fresh state database raced on migrations; migrations now
  run under an exclusive lock.
- Agent file edits invalidate stale Python bytecode.

## [0.2.0] - 2026-09-27

### Features

- **HighhX Pro — `highhx agent`**: an AI developer agent in the terminal. Describe the task in plain
  language; the agent inspects the project, proposes a plan (approve / revise), works through
  HighhX's own capabilities as tools — code search and edits, tests, lint/type checks, fixers,
  builds, dependencies, git, security scans, doctor/diagnose/repair, workflows, deployments and
  rollbacks — verifies the result and summarises it. Live progress (✓ / ✗ per step), streamed
  Markdown answers, diffs for every file change, `/undo`, and slash commands (`/help`, `/status`,
  `/plan`, `/context`, `/model`, `/mode`, `/history`, `/changes`, `/memory`, `/usage`, `/clear`, `/quit`).
- The agent reuses the existing safety system: every command goes through `Engine.run` (risk
  classification, `policies.yaml`, approvals, history); agent actions have policy names
  (`agent:write`, `agent:exec`, `agent:deploy:<target>` …); approval modes `ask` (default),
  `auto-edit` and `read-only`; deployments and rollbacks always ask.
- Provider abstraction with streaming adapters for the managed HighhX gateway (default), Anthropic,
  OpenAI and Google Gemini (bring your own key: `pip install "highhxcli[ai]"`).
- Sessions and transcripts are saved (`highhx agent --continue`, `--resume ID`, `highhx agent sessions`);
  per-project memory in `.highhx/memory.md`; project instructions from `HIGHHX.md` / `AGENTS.md`.
- `highhx agent "…"` handles one request non-interactively (pipes, CI, `--json`).
- HighhX platform integration: `highhx login` (browser device flow or `--with-token`), `highhx logout`,
  `highhx account` (`status`, `plans`, `usage`, `upgrade`, `billing`, `settings`).
- `agent:` section in `.highhx/config.yaml` (provider, model, approval, max_steps, effort, instructions).
- A bare `highhx` now suggests the next step (`highhx init`, or signing in for Pro).
- New exit code 10: HighhX account required, or the plan does not include the feature.
- `server/`: the HighhX Platform backend (FastAPI + SQLAlchemy; SQLite or PostgreSQL) — accounts,
  device sign-in, hashed API tokens, Free/Pro entitlements, the metered AI gateway, projects,
  agent-session sync, Stripe checkout/portal/webhooks, rate limiting and security headers.

### Security

- The agent never reads or writes secret files (`.env*`, private keys, credential files), cannot
  leave the project root (symlinks included), cannot modify `.git/` or HighhX state, and honours
  `forbidden_files`. Tool output is redacted before it reaches the model or the transcript.
- Platform credentials are stored per user with mode 0600; plain http is refused except for localhost.

## [0.1.0] - 2026-09-26

### Features

- Project detection for Python, Node.js, React, Next.js, Flutter/Dart, Java, C/C++, Go, Rust, Docker, databases and monorepos.
- `highhx init` with per-stack templates for config, environment, policies and workflows.
- Execution engine: streaming output, timeouts, cancellation, retries with exponential backoff, parallel execution, graceful shutdown.
- Workflow engine: dependency graphs, parallel stages, conditions, variables, outputs, approvals, retries, timeouts, reusable workflows, dry-run, validation and graphs.
- Risk classification and approvals with non-bypassable rules; project policies.
- Environment profiles with `.env` files, validation and masked secrets.
- Dependencies, testing (with coverage/changed/watch), building, artifacts and cleanup.
- Git helpers, semantic versioning, changelog generation from Conventional Commits, releases and publishing.
- Deployment targets (local, Docker, SSH, Kubernetes, Terraform, plugins) with preflight, health checks, state tracking and rollback.
- Local security checks: secrets, permissions, configuration, workflows, policies and dependency audits.
- Services, ports, Docker Compose and database (SQLite, PostgreSQL, MySQL) management.
- Plugins with manifests, declarative contributions and integrity-checked code.
- Execution history, redacted logs, tracing, reports, doctor, diagnose and repair.

### Security

- Plugin code runs only for contents trusted per user (`highhx plugin trust`); a repository cannot enable its own plugin code. Plugins containing symlinks are rejected and plugin names are validated.
- All output — including `--json` documents and captured command output — is redacted.
- Isolated plugin commands never receive environment-profile values.
- Values substituted into deploy commands are restricted to a safe character set.
- Hardened risk rules (wrapped shells, split `rm` flags, fork bombs, raw device writes, `find -delete`, `docker rm`, `shred`/`truncate`).
- Duplicate YAML keys are rejected.

### Reliability

- Workflow step output is written to execution logs; retries, step transitions and approval decisions are logged.
- `--json` always emits exactly one JSON document.
- An unreadable state database is diagnosed and can be repaired (`highhx repair`).
- `mysqldump` runs with `--no-tablespaces`, so backups work without the PROCESS privilege.
- Global `--profile` was renamed `--config-profile` so it no longer collides with `env --profile`.
