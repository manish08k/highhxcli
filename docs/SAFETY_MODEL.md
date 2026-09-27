# Safety model

HighhX decides what is safe with deterministic rules — never with a model. Code:
`src/highhx/actions/policy.py`, `src/highhx/safety/`, `src/highhx/approvals/`.

## Risk levels

| Risk | Examples | Approval |
|---|---|---|
| **SAFE** | git status, git diff, read a file, project status, search | never asked |
| **LOW** | run tests, checks, build, start services, dependency audit | your own: runs · the agent: asks in `ask` mode, runs in `auto-edit` |
| **MEDIUM** | write/copy/move a file, git commit, tag, pull, install/update packages, formatters | always asked; your own may be pre-approved with `--yes` or `/approve` |
| **HIGH** | git push, deploy (non-production), database migration, delete a file | always asked; your own may be pre-approved with `--yes` or `/approve` |
| **CRITICAL** | production deploy, database restore, `DROP DATABASE`, `rm -rf`, force-push, `curl … \| sh` | type `approve` in a terminal; nothing pre-approves it |
| **blocked** | `rm -rf /`, `mkfs`, deleting the filesystem root | never runs through automation |

The AI agent can never pre-approve anything: `--yes` and `/approve` apply only to your own
deterministic actions.

## How the effective risk is decided

```text
effective risk = max(
    catalog floor          the action's declared minimum (database.migrate is HIGH),
    classifier verdict     SafetyPolicy on the concrete action (command semantics, path,
                           environment, UI element role/type/label),
    action-level rules     recursive forced delete → CRITICAL
)
```

The classifier parses commands into segments (`&&`, `;`, `|`, `sh -c`, `sudo`) and rates each
program by what it does: git push/force-push, destructive SQL through database clients,
`terraform destroy`, package installs, credential and permission changes, downloads piped
into a shell, remote code execution. UI actions are rated from the element's role, type and
label in several languages (submit, pay, delete, send, password and card fields). Files are
rated by what the path controls (policies, CI, hooks). Deployments by environment
(`production`/`prod` → critical).

Project policy (`.highhx/policies.yaml`) is applied on top: `deny`, `require_approval`
with a risk and `bypassable: false`, protected branches, forbidden files.

## Approval semantics

* **Bound to the exact action.** A confirmation issues a single-use HMAC ticket for the
  action's canonical digest; the executor redeems it against the action as it is about to
  run. Changing anything (target, command, environment) invalidates it.
* **Preview, then approve.** `/plan <request>` shows every step with its risk; `/approve`
  runs exactly those planned actions (medium/high pre-approved, critical still typed);
  `/deny` discards them.
* **Non-interactive means deny.** Without a terminal, anything that asks is denied (exit 6),
  unless `--yes` covers it — never for critical, never for the agent.
* **Read-only mode** (`--mode read-only` for the agent) refuses everything above SAFE.

## Never executed blindly

* Ambiguous or open-ended requests are not resolved on Free — no action is guessed, no half
  of a compound request runs.
* `deploy` without a named target does not resolve from plain language.
* Speech transcripts are shown and confirmed before they run.
* Destructive shell commands are classified by structure, not by a keyword list alone.

## Dry run and previews

`--dry-run` plans every action (`status: planned`, with its risk and reasons) and executes
nothing. File changes by the agent show a diff before approval; confirmations show action,
target, resource, tool, command, risk and the reasons.

## Retries

Only **idempotent** actions (reads, status, logs, scans) are retried, only on transient
failures, with exponential backoff. Deterministic failures (invalid input, confinement) are
never retried. Non-idempotent actions — writes, commits, pushes, deploys, installs, shell
commands — are never retried automatically, and neither are the agent's tools. Model calls
(Pro) retry with bounded backoff and a circuit breaker.

## Rollback

See [ACTION_ENGINE.md](ACTION_ENGINE.md#compensation-rollback) for which actions can be undone
and how. `/undo` restores the last file changes; graphs and workflows with rollback undo
completed steps newest-first; compensations that are themselves risky actions ask again.

## Confinement

File actions (Free and Pro) resolve paths inside the project root (symlinks resolved), refuse
secret files (`.env*`, private keys, credential files, data files named like secrets),
refuse `.git/` and HighhX state, and honour `forbidden_files`. File search follows
`.gitignore` (git's own rules).
