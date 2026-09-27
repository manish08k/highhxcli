# HighhX Pro: the AI developer agent

HighhX has exactly two plans:

| | |
|---|---|
| **Free** | The interactive `highhx` session with local capabilities: known plain-language requests run deterministically (no AI), every CLI command, `!shell` commands with approvals, workflows, diagnostics, build/test, git, deploy, deterministic browser/desktop automation (`highhx computer`, `highhx do`). No AI, no account needed. |
| **Pro** | The same session **plus** the AI agent: natural-language tasks, AI planning, adaptive multi-step execution, recovery and replanning, and AI computer use. |

Free and Pro share one interface and one execution platform: the same session, tools,
safety policy, human confirmation, verification, audit trail and engine. Pro adds an
intelligence layer that *chooses* actions; it never gets its own way to execute them.

## Capabilities

`highhx.cloud.capabilities` decides what a session can do. Local capabilities
(`local.commands`, `local.shell`, `local.intents`, `local.automation`) are always
there. Platform capabilities are the plan features (`agent`, `agent.code_changes`,
`agent.commands`, `agent.git`, `agent.deploy`, `agent.computer_use`, `cloud.sessions`)
and exist only when the HighhX platform reports them for your account. The result
shapes the CLI only: the platform checks the plan on every AI request, so nothing
local can unlock a Pro capability. Only a live answer from the platform attaches the
agent: the locally cached account copy is shown (`Pro • Offline (cached)`) but grants
nothing, and `create_session` — the one constructor of the agent runtime — refuses it.
While the platform is unreachable the session keeps its local capabilities and re-checks
the platform (at most every 20 seconds) when a request needs the agent; when the platform
is back and grants Pro, the agent attaches and handles that request. If the gateway fails
during a Pro request, the session says so and offers the local route for it.

Free never calls a model, never reads a provider API key (there is no bring-your-own-key
path; provider keys live only on the platform) and never constructs the agent runtime —
`tests/unit/agent/test_free_pro_boundary.py` checks this with tripwires on every AI entry
point, a static scan of the package and an instrumented interpreter.

Requests that need a capability the session lacks show a **HighhX Pro capability**
panel naming it (AI code changes, AI git operations, AI-driven deployment, AI browser
and desktop automation, or the agent itself) with the HighhX commands that can do part
of the job locally — `[1-3] Continue locally` runs one of them, `[p] View Pro` shows the
plans. Nothing is blocked that Free can do: known requests run directly.

## Starting

```bash
highhx login                                   # once (Pro)
highhx                                         # interactive session in this project
highhx agent                                   # the same session
highhx agent "fix my failing tests"            # interactive, starting with this request
highhx agent --continue                        # continue the last session in this project
highhx agent --resume s-20260927T101500-3f9a1c
highhx agent sessions                          # saved sessions (yours only)
highhx agent stop                              # kill switch: stop running agents in this project
```

Outside a terminal (pipes, CI, `--json`) the agent handles one request and exits:

```bash
echo "run all the tests and fix whatever fails" | highhx agent --yes --mode auto-edit
highhx agent --json --mode read-only "find security issues" | jq -r .text
```

Exit codes: `0` completed, `1` stopped early (step limit, output limit, refusal,
cancellation, provider failure), `10` no HighhX Pro account. (One-shot requests are AI
requests, so they need Pro; the interactive session works on every plan.)

## How a request runs

```text
request → model (via the HighhX gateway) → tool calls
        → validate → path confinement → safety classification → project policy
        → approval / confirmation ticket → HighhX engine → verify → audit
        → results (framed as untrusted data) → model → … → summary
```

For multi-step work the agent proposes a plan you approve (or revise) and reports each
step live (✓ / ✗). Every tool result says whether it was **verified** — a file re-read
after writing, an exit code checked, a UI observed again — and success is never
reported just because an action was dispatched.

### Tools

| Area | Tools |
|---|---|
| Understand | `project_overview` `list_files` `read_file` `search_code` `git_status` `git_diff` `git_log` `recent_runs` `doctor` `diagnose` `dependencies` `security_scan` `deploy_targets` `list_workflows` |
| Plan & remember | `propose_plan` `update_plan` `remember` |
| Change & verify | `edit_file` `write_file` `delete_file` `run_tests` `run_checks` `run_fix` `build` `install_dependencies` `run_command` `run_workflow` `repair` |
| Git & delivery | `git_commit` `git_branch` `git_push` `deploy` `rollback` |
| Computer use | `computer_observe` `computer_act` `browser_open` `app_open` — see [computer-use.md](computer-use.md) |

Each tool has a JSON schema (inputs are validated before anything runs), a timeout, a
cancellation token, structured results (`ok`, `error_code`, `verified`) and an audit
record. Only these tools exist for the model; any other name is refused.

## Safety

The safety layer is deterministic and independent of the model:

- **Classification** by action type, target, scope, environment and reversibility —
  commands are parsed (`sh -c`, `sudo`, pipelines), so `git push --force`, `DROP TABLE`
  through `psql`, `curl … | sh`, package installs, credential and permission changes,
  security controls (`ufw disable`, `--no-verify`) and production targets are recognised;
  UI controls are classified by role/type/structure (submit buttons, forms with passwords,
  destructive styling, sensitive links) and by label in several languages.
- **Explicit confirmation** for sensitive or irreversible actions (submit, pay, delete,
  install, publish, send, deploy, credential/permission changes …). The panel shows the
  exact action, target, resource, tool, command, risk and reason; critical actions require
  typing `approve`. `--yes` never confirms the agent's sensitive actions.
- **Approvals are bound to the action.** A confirmation issues an HMAC ticket over the
  action's canonical description (for files: including the content hash; for UI controls:
  role, name and position). It is redeemed once, right before execution, against the action
  as it is about to run — approving "delete a.txt" can never authorise "delete b.txt" or
  another command, and a UI that changed after approval invalidates it.
- **Blocked outright:** catastrophic commands (`rm -rf /`, disk formatting …); the agent may
  never type passwords, one-time codes or payment details.
- **Approval modes** (`--mode`, `/mode`, `agent.approval`): `ask` (default) asks before
  normal changes too; `auto-edit` runs normal changes without asking (sensitive ones still
  ask); `read-only` offers no change tools.
- **Confinement:** only files inside the project (symlinks resolved); never `.env*`, keys or
  credential files; never `.git/` or HighhX state; `forbidden_files` honoured; changes to
  policies, CI or hooks always need confirmation.
- **Prompt injection:** only your messages are instructions. File contents, command output,
  web pages and UI text reach the model inside an `<untrusted-data>` envelope, and text that
  looks like instructions to an AI is flagged. Policy, entitlements and tool authorisation are
  enforced in code and cannot be changed by content.
- **Secrets:** tool output, transcripts and the audit log are redacted (known secret values,
  provider/platform tokens, auth headers, cookies, private keys, password assignments,
  payment cards).
- **Policy:** agent actions have names for `.highhx/policies.yaml` — `agent:write`,
  `agent:delete`, `agent:exec`, `agent:test`, `agent:check`, `agent:fix`, `agent:build`,
  `agent:git-commit`, `agent:git-push`, `agent:deploy:<target>`, `computer:click` …

`highhx audit` lists every decision (allowed / confirmed / denied / blocked), its outcome
and whether it was verified.

## Reliability

- **Model calls** are retried on transient failures (network, 429, 5xx, truncated streams)
  with exponential backoff and jitter, at most 4 attempts, each with a fresh idempotency key;
  authentication and request errors — and an upstream the platform reports as not configured
  (`provider_unavailable`) — are not retried. After repeated failures a circuit
  breaker pauses requests for 30 s (deterministic commands keep working).
- **Tools are never retried automatically.** A retry of a model call happens before any tool
  of that step runs; a failed tool is reported to the model, which decides what to do. A
  sensitive UI action that failed is not repeated without a new decision.
- **Streaming resumes.** If the connection to the platform drops mid-response (not when the
  platform answers with an error), the CLI
  reconnects with the same idempotency key and `Last-Event-ID`; the platform replays only the
  missed events — the model is not called again and usage is not charged twice.
- **Upstream providers** (on the platform, or self-hosted): each call has an inactivity
  timeout (300 s); SDK-level retries happen only before a response starts streaming, so no
  output is ever duplicated; a stream that ends without its final stop signal is treated as a
  failed (retryable) call, never as a complete answer; transport errors are normalised.
- **Cancellation** (Ctrl+C, `highhx agent stop`, SIGTERM) propagates to the model stream
  (closed locally and cancelled on the platform), the retry loop, running tools and their
  child processes, and the browser connection. A cancelled turn is saved as `cancelled`.

## Sessions

Sessions move through `created → running ⇄ waiting_for_confirmation → completed | failed |
cancelled → closed` (a closed session can be resumed). Invalid transitions are rejected on the
CLI and on the platform. One process at a time may drive a session (a lease, reclaimed if the
holder died). Transcripts stay in the project's state database (redacted); only metadata
(title, provider, model, turns, token usage, status) is synced to your account. Sessions are
scoped to the signed-in account.

## Providers

All Pro AI goes through the HighhX platform, which authenticates you, checks the Pro
entitlement, applies your allowance and meters usage server-side. You choose which upstream
the gateway uses: `highhx` (platform default), `anthropic`, `openai` or `gemini`
(`--provider`, `/model`, `highhx account settings`). The CLI never talks to a provider
directly and needs no provider key; self-hosters configure keys on their platform.

## Configuration

```yaml
# .highhx/config.yaml
agent:
  provider: highhx          # highhx | anthropic | openai | gemini  (routing preference)
  model: claude-opus-5
  approval: ask             # ask | auto-edit | read-only
  max_steps: 60             # capped by your plan
  effort: high
  instructions: "Use pnpm, never npm."
  sync_sessions: true
```

Project instructions are also read from `HIGHHX.md` / `AGENTS.md`; durable facts the agent
learns go to `.highhx/memory.md`.

## Slash commands

`/help` `/status` `/plan` `/context` `/model [provider|model]` `/mode [ask|auto-edit|read-only]`
`/history` `/changes` `/undo` `/memory [clear]` `/usage` `/clear` `/quit`. Ctrl+C cancels the
current task; press it twice at the prompt to exit.
