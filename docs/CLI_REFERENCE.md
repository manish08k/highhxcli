# CLI reference

The complete, generated reference of every command and option is
[commands.md](commands.md). This page covers the interactive session and the commands you
will use most.

## Starting

| Command | What it does |
|---|---|
| `highhx` | The interactive session (in a terminal); prints help when piped or with `--json` / `--quiet` |
| `highhx "request"` | A plain-language request — the same as `highhx agent "request"`: in a terminal, the session with it as the first message; otherwise (Free) the deterministic plan runs and exits (0 all steps verified · 1 a step failed · 10 open-ended: Pro). Real command names always win; a single unknown word is a usage error |
| `highhx do [--plan] "request"` | Run a request deterministically (no AI) and exit; `--plan` shows the deterministic decision and the JSON action plan without running (`--json` for the data) |
| `highhx agent ["request"]` | The same session (a request becomes the first message); one-shot AI request outside a terminal (Pro) |
| `highhx agent --verify test[,check,build] "goal"` | Pro, headless: an autonomous task; exit 0 only when HighhX verified it (`--attempts N`, `--json` for the report) |
| `highhx voice` | The session with voice on |

Global options work everywhere: `--json`, `--dry-run`, `--yes/-y`, `--force`, `--quiet/-q`,
`--verbose/-v`, `--debug`, `--no-color`, `--cwd/-C DIR`, `--config-profile NAME`.

## In the session

| Input | Purpose |
|---|---|
| plain language | Free: the deterministic resolver plans it (open Gmail, play lofi on YouTube, switch to Slack, run the tests …) and each step runs and is verified; unknown names are explained; open-ended requests show the Pro panel. Pro: the AI agent |
| `!command` | A shell command as the `shell.run` action (classified, approved by risk, audited) |
| `highhx <command>` | Any HighhX command, in place |
| Enter on an empty line | Talk (when voice is on) |
| `\` at line end, or `"""` … `"""` | Multi-line input |
| ↑ / ↓, Ctrl+R | Input history (kept across sessions) |
| Ctrl+C | Interrupt the running request (twice at the prompt: exit) |
| Ctrl+D | Exit |

### Slash commands

Grouped as in `/help`. `/status` is the hub: it shows a pending plan (→ `/approve` or
`/deny`), the last failure (→ `/retry`), running workflows (→ `/cancel`) and what this session
did (→ `/history`, `/changes`).

| Group | Command | Purpose |
|---|---|---|
| Run | `/plan <request>` | Preview the steps, their risk and approvals — runs nothing |
| | `/approve` · `/deny` | Run the previewed plan exactly (critical steps still ask) · discard it |
| | `/retry` | Run the last request that failed again (approvals ask again) |
| | `/run <action> [k=v …]` | Run one catalog action, e.g. `/run filesystem.read path=README.md` |
| | `/task <goal> [--verify test,check,build] [--attempts N]` | Pro: work until HighhX verifies the checks (requests that say "…make sure all tests pass" do this automatically) |
| Review | `/status` | Plan, project, pending work and this session at a glance |
| | `/history` | What this session ran: ✓ done · ✗ failed · ⊘ not approved |
| | `/changes` · `/undo` | Files created, modified or deleted in this session · revert the most recent change |
| | `/doctor` | Check tools, configuration, environment variables and ports |
| Workflows | `/workflows` · `/workflow <sub> …` | List · create, list, run, inspect, runs, resume, cancel |
| | `/resume [run-id]` · `/cancel [run-id]` | Resume the last failed or cancelled run · cancel a run in another terminal |
| Project | `/init` | Set up HighhX here: config, policies, workflows, history |
| | `/context` · `/tools [category]` · `/config` · `/memory [clear]` | Project facts · capabilities and actions · configuration · project memory |
| Account & AI | `/login` · `/account` · `/pro` · `/usage` | Sign in · your plan · what Pro adds · AI usage |
| | `/model [name]` · `/mode [ask\|auto-edit\|read-only]` | Pro: the AI model · the agent's approval mode |
| Session | `/voice [on\|off\|mute\|status]` · `/clear` · `/help` · `/quit` | Voice · new conversation · commands · exit |

## Everyday commands

| Command | Purpose |
|---|---|
| `highhx init`, `status`, `info`, `doctor`, `diagnose`, `repair` | Set up and inspect a project |
| `highhx test`, `check`, `fix`, `build`, `dev` | Develop |
| `highhx git status\|diff\|commit\|branch\|tag\|sync\|history` | Git with policies |
| `highhx deps install\|update\|outdated\|audit` | Dependencies |
| `highhx deploy to TARGET`, `rollback`, `deploy status\|logs` | Deployments |
| `highhx security scan` | Secrets, permissions, config, policy, workflows, dependencies |
| `highhx actions list\|show\|plan\|run` | The action catalog from the command line |
| `highhx workflow run\|inspect\|runs\|resume\|cancel\|validate\|graph\|create\|list` | Workflows |
| `highhx schedule`, `trigger`, `hook`, `watch` | Starting workflows automatically |
| `highhx history`, `events`, `audit`, `logs` | What happened (see [OBSERVABILITY.md](OBSERVABILITY.md)) |
| `highhx runs [list\|show [RUN_ID]\|stats]` | Plain-language automation runs: the deterministic decision, the plan, each step's result and verification; totals, failures, Pro escalations, most-used actions and targets |
| `highhx computer status` | Browser, Accessibility, the automation engine in use (C#/.NET or Python) and the target registry |
| `highhx login`, `logout`, `account` | HighhX account and plan |
| `highhx plugin …`, `config …`, `policy …` | Extensions, configuration, policies |

## Environment

| Variable | Purpose |
|---|---|
| `HIGHHX_AUTOMATION_ENGINE` | `python`, `dotnet` or a path to `highhx-automation`; default: the .NET engine when installed, else Python ([engine/dotnet](../engine/dotnet/README.md)) |
| `<config dir>/targets.yaml` | Your own websites, applications and project places ([FREE_AUTOMATION.md](FREE_AUTOMATION.md#the-target-registry)) |

## Exit codes

`0` success · `1` failure · `2` usage error · `3` configuration / not initialized · `4` not
found · `6` not approved · `7` blocked by policy · `9` security findings · `10` HighhX Pro required ·
`124` timeout · `130` cancelled. Full table: [commands.md](commands.md#exit-codes).
