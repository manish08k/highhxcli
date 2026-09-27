# Security model

HighhX runs commands on your machine with your privileges. Its job is to make risky
actions visible and deliberate, and to keep secrets out of places they don't belong.

## Risk levels

| Level | Examples | Default behaviour |
|---|---|---|
| SAFE | `git status`, reading files, `highhx status` | runs |
| NORMAL | running tests, installing packages, starting services | runs (`auto_approve: normal`) |
| DANGEROUS | `git push`, `rm -rf build`, `kubectl apply`, staging deploys, broad dependency updates | asks `[y/N]` |
| CRITICAL | force-push, `DROP TABLE`, `terraform apply`, production deploys, database restore, `npm publish` | asks you to type a word |

Rules for commands live in `approvals/rules.py`; add your own under `approvals.rules`.
Some built-in rules are **non-bypassable** (`rm -rf /`, `mkfs`, `terraform destroy`,
`shutdown`): they always need interactive confirmation.

## Approvals

- `--dry-run` shows what would happen and executes nothing.
- `--yes` approves prompts up to `approvals.yes_max_risk` — never non-bypassable ones.
- Without an interactive terminal HighhX **denies** instead of guessing (exit code 6).

## Policies

`.highhx/policies.yaml`:

```yaml
version: 1
protected_branches: [main, "release/*"]   # pushes are dangerous; force-push critical & non-bypassable
require_clean_tree: [release, deploy]
allowed_release_branches: [main]
forbidden_files: [".env", "*.pem", "*.key"]   # refused by `git commit`, reported by scans
rules:
  - id: no-prod-from-features
    when: {action: "deploy:production", branch: "feature/*"}
    effect: deny                          # allow | warn | require_approval | deny
  - id: prod-needs-human
    when: {action: "deploy:*", production: true}
    effect: require_approval
    risk: critical
    bypassable: false
```

Conditions (`when`) combine: `action` (glob, e.g. `deploy:*`, `git:push`, `exec:docker`,
`workflow:ci:deploy`), `command` (regex), `branch`, `target`, `profile` (globs) and
`production`. Check the current repository with `highhx policy check`.

## Secrets

- `highhx env` shows secret values as `******** (N chars)`; `env set NAME` prompts without echo.
- Output, logs and history are redacted: values of secret-looking variables
  (`*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `DATABASE_URL` …) and token patterns
  (GitHub, GitLab, AWS, Stripe, Slack, npm, PyPI, JWTs, credentials in URLs, private keys).
- New `.env` files are created with mode `0600` and added to `.gitignore`.
- Everything HighhX prints — messages, tables, streamed output and `--json` documents,
  including captured command output — passes through the redactor.
- `highhx security secrets` reports type, file, line and length — never the value.
  Suppress a false positive with a `highhx:allow-secret` comment on the line or by adding
  the finding's fingerprint to `security.allowlist`.

## Security checks

`highhx security` (exit 9 when findings reach `--fail-on`, default `high`):

| Check | Finds |
|---|---|
| secrets | credentials in committed files |
| permissions | world-writable files, readable private keys / `.env` files, writable hooks (POSIX) |
| config | auto-approving dangerous actions, credentials in config, code plugins enabled, production targets without health checks, `.env` not git-ignored, debug enabled in production |
| policy | forbidden files committed to git |
| workflows | dangerous steps without `approval`, downloads piped into a shell |
| dependencies | known vulnerabilities via local tools (pip-audit, npm/pnpm/yarn audit, cargo audit) |

Checks report concrete findings only. **An empty report does not mean the project
is secure**, and HighhX never claims that it is.

## Plugins

See [plugins.md](plugins.md#code-plugins): code plugins need `plugins.allow_code: true`
**and** a per-user trust record for their exact contents (SHA-256), created by
`highhx plugin install` or `highhx plugin trust`. A repository cannot grant that trust
itself. Plugins with symbolic links are rejected, and plugin names are validated so
`plugin remove`/`update` can never touch paths outside the plugins directory.

## Command injection

- Commands given as a list of arguments never go through a shell.
- Values substituted into deploy commands (`{{ version }}` …) are restricted to
  letters, digits and `. _ + / : @ -`; anything else is rejected before running.
- `workflow validate` warns when a step interpolates another step's runtime output
  (`${{ steps.x.outputs.y }}`) into its command line — pass it through `env:` instead.
- Every command is risk-classified *after* variables are substituted.

## Automation safety (Free and Pro)

`highhx computer`, `highhx do` and the agent share one deterministic safety layer
(`highhx/safety`): commands and UI actions are classified semantically; sensitive or
irreversible actions (submit, pay, delete, install, publish, send, deploy, credential and
permission changes, security controls, production targets, destructive SQL) need explicit
confirmation bound to the exact action by a single-use HMAC ticket; catastrophic actions are
blocked; every decision is written to a redacted audit log (`highhx audit`). Details:
[agent.md](agent.md#safety) and [computer-use.md](computer-use.md).

## The AI agent (HighhX Pro)

`highhx agent` adds no new execution path: commands run through the same engine
(risk → policy → approval → execute → history). In addition the agent is confined to
the project root (symlinks resolved), never reads or writes secret files (`.env*`,
private keys, credential files), cannot modify `.git/` or `.highhx/state`, honours
`forbidden_files`, and asks before normal-risk changes in the default `ask` mode.
Agent actions have policy names (`agent:write`, `agent:exec`, `agent:deploy:<target>` …).
Everything sent to the model is redacted first. Details: [agent.md](agent.md#safety).

## Data

Free commands keep everything local: `.highhx/state/` (SQLite) and `.highhx/logs/`
(text files), both git-ignored. HighhX makes network requests only when you ask for
something that needs the network (health checks you configured, installing a plugin
from a git URL, package managers you invoke).

With HighhX Pro, `highhx agent` sends the conversation — your requests, the project
overview, and the (redacted) file contents, command output and UI text the agent reads — to
the HighhX platform's AI gateway, which forwards it to the configured model provider. Session metadata (title, model,
token counts, status) is stored in your HighhX account; transcripts stay in the
project's local state database. Platform credentials live in your user config
directory with owner-only permissions.
