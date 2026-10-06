# Security model

HighhX runs commands on your machine with your privileges. Its job is to make risky
actions visible and deliberate, and to keep secrets out of places they don't belong.
How risk and approval are decided is specified in [SAFETY_MODEL.md](SAFETY_MODEL.md);
the plan boundary in [FREE_PRO.md](FREE_PRO.md).

## Threat model

| Threat | Mitigation |
|---|---|
| A risky command runs by accident | Deterministic classification, five-level approval table, typed confirmation for critical, blocked catastrophic actions |
| Approval of one thing runs another | Single-use HMAC tickets bound to the action's canonical digest |
| The AI agent is manipulated by content it reads (prompt injection) | Tool output framed as untrusted data; the agent cannot pre-approve; risk decided by HighhX; secrets never sent |
| Automation escapes the project | Path confinement (symlinks resolved), `.git/` and HighhX state protected, `forbidden_files` |
| Secrets leak into output, logs, events or the model | Redaction of everything printed, logged, recorded or sent; secret files never read |
| A local file grants paid features | Platform capabilities only from a live platform answer; the gateway checks the plan on every request |
| A plugin is more powerful than it declares | Declared risk only raises the floor; plugin actions never reach the agent; code plugins need per-user trust of their exact contents |
| A spoken command is misheard | Transcripts are confirmed before anything runs; approvals still apply |

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
`workflow:ci:deploy`, `action:browser.*`), `command` (regex), `branch`, `target`, `profile`
(globs), `production`, `host` and `app`. Check the current repository with `highhx policy check`.

`require_approval` asks for the action every time, at the rule's `risk`, with the rule's message
as the reason; `--yes` answers it only when the rule is `bypassable`. A blocked action's error
includes the rule's message.

### Sites and applications

```yaml
rules:
  - id: no-banking
    when: {action: "action:browser.*", host: bank.example}     # the site and its subdomains
    effect: deny
    message: HighhX does not operate the bank's site
  - id: mail-is-asked
    when: {action: "action:browser.*", host: "*.mail.example"} # a glob: subdomains only
    effect: require_approval
  - id: no-typing-in-keychain
    when: {action: "action:computer.*", app: "Keychain*"}
    effect: deny
```

Executor actions are named `action:<name>` in rules (`action:browser.click`,
`action:computer.type`); a few name their own (`network:<host>` for `api.request`, `vm:*`).
`host` is the web site a browser action acts on: a navigation's destination, otherwise the page in
front — so a rule against a site also holds for clicks and typing after a link took the browser
there, not only for addresses typed in. `bank.example` matches it and its subdomains, never a
substring (`bank.example.evil.com` is not it); a pattern with `*` is a glob. `app` is the
application a desktop action acts on: the one it names, else the frontmost one (glob,
case-insensitive). Both facts are looked up only when a rule uses them. A site or application
HighhX cannot determine (no page open, no desktop access) matches no rule.

## Screenshots

Every screenshot HighhX takes — page captures and browser screenshots, desktop screen, window and
region captures — has the fields HighhX classifies as secret blacked out before it is stored,
shown to a model or saved as an artifact: password inputs and fields whose `autocomplete` says
password, card (`cc-…`) or one-time code; on the desktop, secure text fields (macOS, Windows UI
Automation `IsPassword`, AT-SPI password text). Browsers draw a password as bullets, but card
numbers, one-time codes and a password shown with "show password" are drawn in clear. A capture
says how many fields it blacked out (`redacted`).

Limits, stated plainly: this is **not** general PII detection — text that merely looks sensitive
(an API key printed on a page, a private message) is not found. Fields inside cross-origin frames
are not seen; on the desktop only the frontmost application's (and the captured window's)
secure fields are known, and only where accessibility is granted. The live view (a screencast to
the person's own loopback console, never stored) is not redacted. Cost: one DOM query (~1.4 ms)
per browser capture; when a secret field is on screen, decoding and re-encoding the image
(~165 ms for 1280×813 in real Chrome).

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

## Computer use

Everything in the computer-use runtime goes through the same executor, gate, policies and
audit ([COMPUTER_USE_ARCHITECTURE.md](COMPUTER_USE_ARCHITECTURE.md)).

- **One path.** The agent loop, specialists, browser replay, benchmarks, MCP tools and the
  console submit `ActionRequest`s to `ActionExecutor`. Drivers are only reached from action
  handlers after approval.
- **Models propose, HighhX decides.** Planner output is parsed into a closed set of verbs and
  catalog actions and checked against the task's allowed actions. A vision model returns
  candidate boxes, never clicks.
- **A label can only raise risk.** A request carries its target's label (`Delete account`),
  which is classified like the control itself, so a point that grounding produced cannot make a
  destructive click look harmless. A caller can raise the floor (`min_risk`), never lower it.
- **Observation is an action.** `computer.state` with pixels is `screen:capture`; a policy can
  deny it. Remote vision raises it to high risk.
- **Policy names for computer-specific risks:** `screen:capture`, `network:<host>`,
  `credential:use`, `android:install`, `android:delete`, `sandbox:create`, `sandbox:exec`,
  `sandbox:apply`. Purchases, sending, deleting and downloads in pages are recognised by the
  existing control classifier (financial, publish, destructive, download categories).
  Shutdown and similar commands go through the command classifier.
- **No silent retries.** Only SAFE or idempotent actions are retried automatically; a risky
  action that failed or may have run is re-planned from a fresh observation and asked again.
  A confirmation declined inside an action (the browser's per-element check) is reported as
  denied and never retried.
- **Secrets.** Typed text is replaced by its length in events, traces, trajectories and audit.
  Text typed into a password, card or one-time-code field is registered with the redactor
  before the action runs, never recorded by the browser recorder (it becomes a variable), never
  stored in a checkpoint, and such a step is never reported verified.
  `api.request` credentials come from environment variables and are redacted.
  `tests/security/test_computer_use_secrets.py` checks every file, database, event and
  dashboard for a typed password and a request credential.
- **Sandboxes** confine writes and network (Seatbelt, bubblewrap, Docker), scrub the
  environment and keep credential folders unreadable. A command run inside one is still
  classified as itself ([SANDBOX.md](SANDBOX.md)).
- **MCP** cannot ask anyone: actions that need approval are refused unless the person starting
  the server passes `--yes` (never for critical or blocked actions).
- **Untrusted content.** Page and screen text given to a model planner is marked as untrusted,
  with an instruction never to follow it. Notes from past trajectories are given as data, not
  instructions, and truncated.
- **URLs.** Query values never reach audit rows, events, network evidence or trajectories:
  URLs there keep parameter names only (`?api_key=…`). For navigations (`browser.open`,
  `browser.extract {url}`) the classifier, the approval prompt and the approval ticket see the
  whole URL; the audit row, its error text and the action's recorded error and summary keep the
  names only (tightened in the October 2026 phase: before, only secret-looking values such as
  `token=` were redacted from navigation audit rows).
- **Where computer-use data lives.** Trajectories, task traces and benchmark results are in
  `.highhx/state/` (git-ignored by `highhx init`, not readable or writable by the agent's file
  tools). State screenshots are kept in the user data directory, at most the 20 most recent.
- **Recording** opens its start page through the executor; the recorder only listens.
- **Model failures** end a task as failed and resumable; they never leave an action half-decided.
- **Audit (tested).** Every new path was checked for driver, adb, browser-input or subprocess
  calls outside action handlers: drivers are instantiated only inside handlers, and the one
  exception found (the recorder navigating by itself) was fixed.
- **Classification follows the inputs.** An action whose nature depends on its inputs says so
  (`ActionSpec.kind_for`): `browser.extract` with a `url` navigates and is classified exactly
  like `browser.open` (privileged schemes such as `file:` and `javascript:` are high risk).
  Before this phase it was rated a plain read (found in the October 2026 audit, regression
  test in `tests/unit/actions/test_state_and_api_actions.py`).
- **Human-verification challenges** (CAPTCHAs) are detected and handed to the person: the agent
  loop stops with `needs_user`. HighhX does not solve, click through, outsource or evade them.
- **Schema extraction** never reads password, card, one-time-code or hidden fields; its
  output is data and passes the redactor like any action output.
- **E-mail** (`email.send`) is high risk and always asked; the password comes only from the
  environment; TLS is required unless the server is on this computer; recipients and subject
  are checked for header injection; the body appears in events and audit only as its length.
- **Workflow loops**: items computed at run time and spliced into a `run:` command line are
  flagged by `workflow validate`, and the resulting command is still classified and asked.
- **Capability report** (`highhx capabilities`) looks only at PATH, the platform and
  configuration: it never contacts a model or the platform and never reads a provider key.
- **Emulators**: starting one is medium risk (high with `wipe`); stopping refuses anything
  that is not an emulator serial.

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

## Event log

`highhx events` reads structured JSON Lines events from the user data directory. Every line
passes through the redactor before it is written; events carry names, statuses and short
summaries — never file contents or environment values. Environment *names* appear in the
project context; their values never do.

## Plugins and the action engine

Declared plugin commands appear as `plugin.<plugin>.<command>` actions for the user only.
Their declared risk can raise the floor but never lower it below LOW, the classifier still
rates the concrete command, they run with the plugin command's isolated environment (no
project secrets) and are never offered to the AI agent.

## Voice

Voice uses local programs only (recorders, a speech-to-text engine you installed with a model
on disk, the OS speech synthesizer). Audio is written to a private temporary directory and
deleted after transcription; nothing is uploaded. See [VOICE.md](VOICE.md).

## October 2026 phase

- **HTTP (SSRF).** `api.request` decides on the address actually dialled, for every hop: link-local and
  cloud-metadata addresses are always refused, private networks need `allow_private` (high risk), and a
  redirect can never move from a public host to this computer or a private one. Before, urllib followed
  redirects anywhere. **Credentials are never forwarded to another host on a redirect** — before,
  urllib forwarded `Authorization` (reproduced, then fixed; `tests/security/test_http_ssrf.py`).
- **Approvals** can be answered from the web console: approve, reject, modify (the new inputs are a new
  plan, classified and asked again), defer, and timeout (= reject); critical actions need the typed word.
- **Web console**: loopback only, per-run token, Host check, CSRF header and Origin check, strict CSP,
  output as text only.
- **Browser profiles** are never read; deleting/importing is high risk. Remote browser tokens are never
  stored. `endpoint` inputs are shown without their query, like URLs.
- **VMs**: `vm.exec` is classified by its command; restore/destroy are high risk.
- **Memory**: preferences only from the person; secrets refused; notes bounded and labelled as data.
- **OCR confidence** is tesseract's own (a constant 0.9 was reported before).
