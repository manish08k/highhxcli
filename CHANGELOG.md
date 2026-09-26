# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/) and the
project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
