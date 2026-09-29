# Command reference

Generated from the CLI's own help text by `scripts/generate_docs.py`. Every command
also accepts the global options below, before or after the command name.

## Global options

| Option | Effect |
|---|---|
| `--version, -V` | Show the version and exit. |
| `--json` | Machine-readable JSON output (no decoration). |
| `--dry-run` | Show what would happen without changing anything. |
| `--yes, -y` | Approve prompts (never bypasses non-bypassable policies). |
| `--force` | Allow overwriting existing files where a command supports it. |
| `--quiet, -q` | Only print errors. |
| `--verbose, -v` | Show more detail (commands being run …). |
| `--debug` | Show tracebacks and debug logging. |
| `--no-color` | Disable colors (also honours NO_COLOR). |
| `--cwd, -C` | Run as if started in DIR. |
| `--config-profile` | Apply the config overlay .highhx/profiles/NAME.yaml (or set HIGHHX_PROFILE). |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Failure (command, step or check failed) |
| 2 | Usage error |
| 3 | Configuration error / not initialized |
| 4 | Not found (workflow, target, execution …) |
| 5 | Required tool missing |
| 6 | Approval denied |
| 7 | Blocked by policy |
| 8 | Validation failed |
| 9 | Findings reported (security, audit) |
| 10 | HighhX account required (sign in with `highhx login`, or the plan lacks the feature) |
| 124 | Timed out |
| 127 | Command not found |
| 130 | Cancelled (Ctrl+C) |

## Interactive session & AI agent (HighhX Pro)

### `highhx agent`

HighhX Pro: an AI developer agent in your terminal.

Describe what you want in plain language — "fix my failing tests",
"explain how this project works", "make this project production ready".
The agent inspects the project, proposes a plan, works through HighhX's
tools (tests, checks, builds, git, security, deploy …) and verifies the
result. Every risky action goes through HighhX's policies and approvals.

Running `highhx agent` without a subcommand runs `highhx agent run`.

#### `highhx agent models`

AI providers and models the HighhX gateway can route agent requests to.

```
highhx agent models [OPTIONS]
```

#### `highhx agent run`

In a terminal, start the interactive HighhX session (the same one as bare
`highhx`; PROMPT becomes the first request). When input/output is not a
terminal, or with --json, handle PROMPT as one AI agent request and exit.

  highhx agent
  highhx agent "why is the application crashing?"
  highhx agent --continue
  highhx agent --mode read-only "find security issues"
  echo "run all the tests and fix whatever fails" | highhx agent --yes --mode auto-edit

Exit code: 0 when the request completed, 1 when it stopped early (step limit,
output limit, refusal, interruption), 10 without a HighhX Pro account
(one-shot requests only; the interactive session works on every plan).

```
highhx agent run [OPTIONS] [PROMPT]...
```

| Option | Description |
|---|---|
| `--continue, -c` | Continue the most recent session in this project. |
| `--resume` | Resume a saved session (see `highhx agent sessions`). |
| `--provider` | AI provider (default: account setting, else highhx). |
| `--model` | Model to use (default: provider default). |
| `--mode` | Approvals: ask (default), auto-edit (normal changes without asking), read-only. |
| `--max-steps` | Maximum tool steps per request. |
| `--effort` | Reasoning effort, where the model supports it. |
| `--verify` | Task mode: work until HighhX verifies these checks (comma list of test, check, build; `none` disables). Default: what the request states (e.g. 'make sure all tests pass'). |
| `--attempts` | Task mode: attempts before giving up (default 3). |

#### `highhx agent sessions`

Saved sessions (newest first). Resume one with `highhx agent --resume ID`.

```
highhx agent sessions [OPTIONS]
```

| Option | Description |
|---|---|
| `--all` | Sessions from every project, not just this one. |
| `--limit` | Maximum sessions to list. (default: `20`) |

#### `highhx agent stop`

Cancel running `highhx agent` processes: the current model request is cancelled
(also on the platform), retries stop, running commands and their child processes are
terminated, and the session is saved as cancelled.

```
highhx agent stop [OPTIONS]
```

| Option | Description |
|---|---|
| `--all` | Stop agents in every project, not just this one. |
| `--session` | Stop the agent working on this session. |

### `highhx voice`

Opens the same session as `highhx`, with voice on: speak, press Enter, and the
transcript runs through the normal HighhX pipeline — the same resolver (Free) or agent
(Pro), risk checks, approvals, verification and audit as a typed request.

Speech-to-text is whisper.cpp, fully local and free (no account, no cloud). The first
time, HighhX offers to install whisper.cpp and an audio recorder and to download a
verified model — see docs/VOICE.md.

highhx voice            the session with voice on
highhx voice status     what is installed and ready
highhx voice setup      install / download what is missing
highhx voice model      show or switch the whisper model
highhx voice test       record and transcribe a sample (runs nothing)

#### `highhx voice model`

Show the whisper.cpp model voice uses and whether it is downloaded, or switch to NAME
(tiny.en, base.en — the default — or small.en). A new model is downloaded by
`highhx voice setup` or the next /voice on.

```
highhx voice model [OPTIONS] [NAME]
```

#### `highhx voice setup`

Install what local voice needs (whisper.cpp and an audio recorder, with Homebrew or the
system package manager), download and SHA-256-verify the whisper model, and check the
microphone. Asks before installing or downloading; --yes agrees in advance.

```
highhx voice setup [OPTIONS]
```

#### `highhx voice status`

Show whether local voice is ready: whisper.cpp, the model, the recorder, the
microphone and spoken replies — and the fix for anything missing. Detection only:
nothing is installed, downloaded or recorded.

```
highhx voice status [OPTIONS]
```

#### `highhx voice test`

Check voice end to end: push-to-talk (speak, then Enter), transcribe with whisper.cpp
and show the transcript and its confidence. Nothing is run. With --file, transcribe a
WAV file instead of the microphone.

```
highhx voice test [OPTIONS]
```

| Option | Description |
|---|---|
| `--file` | Transcribe this 16 kHz WAV file instead of the microphone. |

## Automation (no AI)

### `highhx do`

HighhX Free's deterministic resolver turns the request into
a JSON action plan; each step then runs through the action executor
(risk classification, approval, verification) and the run is traced.

  highhx do run the tests
  highhx do "open Gmail and search internship"
  highhx do "play lofi on YouTube"
  highhx do --plan --json "open my project and run the tests"

`highhx "…"` (without `do`) does the same. Requests that need understanding
("fix whatever is failing") are for the AI agent: HighhX Pro.

```
highhx do [OPTIONS] REQUEST...
```

| Option | Description |
|---|---|
| `--plan` | Show the deterministic decision and the action plan; run nothing. |

### `highhx computer`

Deterministic UI automation: open apps and pages, observe controls by role and
name, click, type, press keys and run flow files — each step is checked by the
HighhX safety policy, sensitive steps (submit, pay, delete …) ask for
confirmation, and every step is verified and recorded in `highhx audit`.

Targets are selectors: `Search`, `button:Search`, `textbox="Email"`, `link:Docs#2`.

| Option | Description |
|---|---|
| `--headless` | Run the HighhX browser without a window (default: visible). |

#### `highhx computer at`

What is at X,Y — application, role, name and bounds (read-only).

```
highhx computer at [OPTIONS] X,Y
```

#### `highhx computer browser`

The HighhX browser uses its own profile (never your personal one) and a DevTools
port bound to 127.0.0.1.

##### `highhx computer browser start`

Start Chrome/Chromium/Edge/Brave for HighhX automation.

```
highhx computer browser start [OPTIONS]
```

##### `highhx computer browser stop`

Close the HighhX browser and forget its session.

```
highhx computer browser stop [OPTIONS]
```

#### `highhx computer click`

Click the control SELECTOR (e.g. `button:Search`) — or, on the desktop, at a point
(`--at 640,400`) or on text found on screen (`--text Save`). Sensitive controls, and every
click at a point, ask first.

```
highhx computer click [OPTIONS] [SELECTOR]
```

| Option | Description |
|---|---|
| `--at` | Desktop: click at this point (desktop points). |
| `--text` | Desktop: click this text on screen (accessibility, then OCR). |
| `--right` | With --at/--text: the right button. |
| `--double` | With --at/--text: a double click. |
| `--source` | browser (DevTools DOM) or desktop (native accessibility, macOS). (default: `browser`) |

#### `highhx computer clipboard`

Print the clipboard's text, or replace it with --set TEXT (read back to check). Asks first:
the clipboard often holds private data.

```
highhx computer clipboard [OPTIONS]
```

| Option | Description |
|---|---|
| `--set` | Replace the clipboard with TEXT. |

#### `highhx computer drag`

Press at X1,Y1, move to X2,Y2 and release (asks first).

```
highhx computer drag [OPTIONS] X1,Y1 X2,Y2
```

| Option | Description |
|---|---|
| `--right` | With the right button. |

#### `highhx computer menu`

Choose PATH (items separated by >) in APP's menu bar.

```
highhx computer menu [OPTIONS] APP PATH
```

#### `highhx computer move`

Move the pointer to X,Y (hovering) and check that it arrived.

```
highhx computer move [OPTIONS] X,Y
```

#### `highhx computer observe`

Observe the browser page or the frontmost application semantically.

```
highhx computer observe [OPTIONS]
```

| Option | Description |
|---|---|
| `--source` | browser (DevTools DOM), desktop (native accessibility, macOS) or screen (local OCR, read-only). (default: `browser`) |
| `--app` | Desktop source: application to inspect (default: frontmost). |
| `--actions` | Also list the valid action ids. |

#### `highhx computer open`

`highhx computer open https://example.com` · `highhx computer open Calculator`.

```
highhx computer open [OPTIONS] TARGET
```

#### `highhx computer press`

Press KEY in the focused control. Enter in a form counts as submitting it.

```
highhx computer press [OPTIONS] {enter|tab|escape|backspace|arrowdown|arrowup|pagedown|pageup|space}
```

| Option | Description |
|---|---|
| `--source` | browser (DevTools DOM) or desktop (native accessibility, macOS). (default: `browser`) |

#### `highhx computer protocol`

The automation protocol every engine speaks — operations, arguments, the version that
introduced each, keys, errors — as JSON. It is also checked in as
schemas/computer-protocol.json (the source for engines written in other languages).

```
highhx computer protocol [OPTIONS]
```

#### `highhx computer quit`

Ask APP to quit (it may ask to save first) and check that it did.

```
highhx computer quit [OPTIONS] APP
```

#### `highhx computer run`

Run the steps in FLOW_FILE (open, click, type, press, select, scroll, expect,
launch, wait). Each step is resolved deterministically, safety-checked,
executed and verified; the flow stops at the first failing step.

```
highhx computer run [OPTIONS] FLOW_FILE
```

| Option | Description |
|---|---|
| `--source` | browser (DevTools DOM) or desktop (native accessibility, macOS). (default: `browser`) |

#### `highhx computer screenshot`

Save a screenshot in HighhX's screenshots folder and print its path. macOS needs Screen
Recording permission for your terminal; HighhX says so instead of saving a blank image.

```
highhx computer screenshot [OPTIONS]
```

| Option | Description |
|---|---|
| `--window` | Only this window (ids from `computer windows`). |

#### `highhx computer scroll`

Scroll the page or window (the desktop with real wheel events).

```
highhx computer scroll [OPTIONS] {up|down|left|right}
```

| Option | Description |
|---|---|
| `--at` | Desktop: scroll the mouse wheel at this point. |
| `--amount` | Desktop: wheel steps. (default: `3`) |
| `--source` | browser (DevTools DOM) or desktop (native accessibility, macOS). (default: `browser`) |

#### `highhx computer select`

Choose OPTION (value or visible text) in the list SELECTOR.

```
highhx computer select [OPTIONS] SELECTOR OPTION
```

| Option | Description |
|---|---|
| `--source` | browser (DevTools DOM) or desktop (native accessibility, macOS). (default: `browser`) |

#### `highhx computer status`

Report native accessibility, browser, OCR and vision availability on this machine.

```
highhx computer status [OPTIONS]
```

#### `highhx computer task`

Work toward a goal in the HighhX browser, step by step:

  TASK → PLAN → OBSERVE → ACTION → RESULT → VERIFY → (RECOVERY) → … → FINAL

The request becomes Task IR (validated JSON): on HighhX Free from the deterministic
resolver, on HighhX Pro — for tasks nobody programmed — from the AI planner, which then
discovers the site from its accessibility tree. Each action is a generic primitive
(navigate, click, type, read, …), checked by the safety policy, executed, verified and
audited; failures are recovered or replanned, never blindly repeated.

  highhx computer task "open YouTube and play lofi"
  highhx computer task --ir task.json
  highhx computer task --schema

```
highhx computer task [OPTIONS] [REQUEST]...
```

| Option | Description |
|---|---|
| `--ir` | Run this Task IR (JSON or YAML) instead of a request. |
| `--show-ir` | Print the Task IR before running it. |
| `--schema` | Print the Task IR JSON Schemas and exit. |
| `--max-steps` | At most this many actions. |
| `--timeout` | Give up after this many seconds. |

#### `highhx computer type`

Replace the content of the field SELECTOR with TEXT (or $VAR with --from-env).

```
highhx computer type [OPTIONS] SELECTOR [TEXT]
```

| Option | Description |
|---|---|
| `--from-env` | Type the value of this environment variable (never shown or logged). |
| `--source` | browser (DevTools DOM) or desktop (native accessibility, macOS). (default: `browser`) |

#### `highhx computer window`

Move and resize APP's front window (or --id WINDOW) to --frame, and check the new frame.

```
highhx computer window [OPTIONS] APP
```

| Option | Description |
|---|---|
| `--id` | The window id (from `computer windows`) instead of APP. |
| `--frame` | The new frame in desktop points. |

#### `highhx computer windows`

Windows front to back with their ids and bounds (for `click --at`, `window`, `screenshot --window`).

```
highhx computer windows [OPTIONS]
```

| Option | Description |
|---|---|
| `--app` | Only this application's windows. |
| `--apps` | List running applications instead. |

## Account

### `highhx login`

Sign in with the browser (device code), or with an API token from stdin:

  highhx login
  echo "$HIGHHX_TOKEN" | highhx login --with-token

New users create their account in the same browser flow. Credentials are
stored in your user config directory (mode 0600), never in the project.
HIGHHX_TOKEN in the environment overrides stored credentials.

```
highhx login [OPTIONS]
```

| Option | Description |
|---|---|
| `--with-token` | Read an API token from stdin (CI, headless machines). |
| `--api-url` | HighhX platform URL (self-hosted / development). |
| `--no-browser` | Print the sign-in link instead of opening a browser. |

### `highhx logout`

Revoke this machine's token on the platform and delete it locally.

```
highhx logout [OPTIONS]
```

### `highhx account`

Your HighhX platform account. Sign in with `highhx login`.

HighhX Free is the full developer CLI; HighhX Pro adds the AI developer agent
(`highhx agent`).

Running `highhx account` without a subcommand runs `highhx account status`.

#### `highhx account billing`

Open the billing portal (change or cancel your subscription, download invoices).

```
highhx account billing [OPTIONS]
```

| Option | Description |
|---|---|
| `--no-browser` | Print the link instead of opening a browser. |

#### `highhx account plans`

What each plan includes. No account needed.

```
highhx account plans [OPTIONS]
```

#### `highhx account settings`

Show or change account-wide agent settings (project `agent:` config and flags override them).

```
highhx account settings [OPTIONS]
```

| Option | Description |
|---|---|
| `--provider` | Default AI provider. |
| `--model` | Default model (e.g. claude-opus-5, gpt-5, gemini-2.5-pro). |
| `--approval` | Default approval mode. |
| `--upstream` | Upstream used by the managed HighhX provider. |
| `--sync` | Sync agent session metadata to your account. |

#### `highhx account status`

Show the signed-in account, its plan and features, and this period's AI usage.

```
highhx account status [OPTIONS]
```

#### `highhx account upgrade`

Start a HighhX Pro subscription through the platform's secure checkout.

```
highhx account upgrade [OPTIONS]
```

| Option | Description |
|---|---|
| `--no-browser` | Print the checkout link instead of opening a browser. |

#### `highhx account usage`

Tokens and requests used through the HighhX AI gateway this period, by model.

```
highhx account usage [OPTIONS]
```

## Project

### `highhx init`

Inspect the project (manifests, lockfiles, Dockerfiles, .git …) and create
.highhx/ with config, environment, policies and default workflows.

Existing files are never overwritten unless you pass --force and confirm.

```
highhx init [OPTIONS]
```

| Option | Description |
|---|---|
| `--stack` | Template to use (python, node, react, nextjs, flutter, java, cpp, docker, monorepo, generic). |

### `highhx status`

Show an overview of the project. Use --json for machine-readable output.

```
highhx status [OPTIONS]
```

### `highhx info`

Everything HighhX detected about this project and its effective commands.

```
highhx info [OPTIONS]
```

### `highhx dev`

Run the `dev` workflow if the project has one, otherwise the dev command
(commands.dev or the detected one) interactively in the foreground.

```
highhx dev [OPTIONS]
```

| Option | Description |
|---|---|
| `--no-workflow` | Ignore .highhx/workflows/dev.yaml and run commands.dev directly. |

### `highhx start`

Start services from `services:` in .highhx/config.yaml (dependencies first),
waiting until each one is healthy.

```
highhx start [OPTIONS] [SERVICES]...
```

### `highhx stop`

Stop services started with `highhx start` (dependents first).

```
highhx stop [OPTIONS] [SERVICES]...
```

### `highhx restart`

Stop, then start the given services (or all).

```
highhx restart [OPTIONS] [SERVICES]...
```

### `highhx check`

Run the `check` workflow if defined; otherwise run the project's lint,
typecheck and test commands in parallel (continuing past failures).

```
highhx check [OPTIONS]
```

## Code & tasks

### `highhx run`

Run WORKFLOW from .highhx/workflows (by file name or `name:`).

Steps run in parallel where dependencies allow; a step never starts before
everything in its depends_on succeeded. --dry-run prints the plan.

```
highhx run [OPTIONS] WORKFLOW
```

| Option | Description |
|---|---|
| `--input, -i` | Workflow input (repeatable). |
| `--env, -e` | Extra environment variable (repeatable). |

### `highhx exec`

Run COMMAND in the project with the active environment profile injected.

The command is classified by risk (e.g. `git push` is dangerous, `rm -rf`
needs approval), recorded in history and its output logged (secrets redacted).

```
highhx exec [OPTIONS] COMMAND...
```

| Option | Description |
|---|---|
| `--timeout` | Kill the command after e.g. 30s, 5m. |
| `--retry` | Total attempts. (default: `1`) |
| `--retry-delay` | Initial delay between attempts (doubles each time). (default: `1s`) |
| `--shell` | Run through the system shell. |
| `--interactive, -i` | Attach the terminal (for prompts / TUIs); output is not captured. |

### `highhx script`

Run script NAME with optional ARGS; without NAME, list scripts.

Sources: `scripts:` in .highhx/config.yaml, package.json scripts, and
[tool.highhx.scripts] in pyproject.toml.

```
highhx script [OPTIONS] [NAME] [ARGS]...
```

### `highhx task`

Run task NAME from `tasks:` in .highhx/config.yaml, running the tasks it
depends on first (in parallel where possible). Without NAME, list tasks.

```
highhx task [OPTIONS] [NAME]
```

### `highhx watch`

Watch files and run COMMAND (or --workflow) on every change.

Without COMMAND or --workflow, all watchers from `watch:` in
.highhx/config.yaml run together. Stop with Ctrl+C. Uses portable polling,
so it works the same on macOS, Linux and Windows.

```
highhx watch [OPTIONS] [COMMAND]...
```

| Option | Description |
|---|---|
| `--workflow, -w` | Workflow to run on change. |
| `--path, -p` | Paths to watch (default: project root). |
| `--pattern` | Only react to matching files, e.g. '*.py'. |
| `--ignore` | Ignore matching files. |
| `--debounce` | Seconds to wait for changes to settle. (default: `0.5`) |
| `--interval` | Polling interval in seconds. (default: `0.5`) |
| `--initial` | Run once at startup. |

### `highhx fix`

Run commands.fix and then commands.format (configured or detected, e.g.
`ruff check --fix` + `ruff format`, `dart fix --apply` + `dart format`).

```
highhx fix [OPTIONS]
```

## Dependencies

### `highhx deps`

Dependency management for every detected ecosystem (pip/uv/poetry,
npm/pnpm/yarn/bun, flutter/dart pub, Maven, Gradle, Cargo, Go).

Running `highhx deps` without a subcommand runs `highhx deps summary`.

#### `highhx deps audit`

Run the ecosystem's local audit tool (pip-audit, npm/pnpm/yarn audit,
cargo audit). Exits with code 9 when vulnerabilities are reported.

```
highhx deps audit [OPTIONS]
```

| Option | Description |
|---|---|
| `--manager, -m` |  |

#### `highhx deps clean`

Remove installed dependencies so they can be reinstalled from scratch.
Shows what will be deleted and asks for confirmation.

```
highhx deps clean [OPTIONS]
```

| Option | Description |
|---|---|
| `--manager, -m` |  |

#### `highhx deps install`

Install dependencies exactly as declared: `npm ci` when a lockfile exists,
`uv sync`, `poetry install`, `flutter pub get` … Lockfiles are not upgraded.

```
highhx deps install [OPTIONS]
```

| Option | Description |
|---|---|
| `--manager, -m` | Only this package manager / ecosystem. |

#### `highhx deps outdated`

Ask each package manager which dependencies have newer versions.

```
highhx deps outdated [OPTIONS]
```

| Option | Description |
|---|---|
| `--manager, -m` |  |

#### `highhx deps summary`

List the package managers detected in this project, whether each tool is installed,
its lockfile, and the command `deps install` would run.

```
highhx deps summary [OPTIONS]
```

#### `highhx deps update`

Update PACKAGES (or everything). The list of outdated packages is shown
and confirmation is required before a broad update rewrites the lockfile.

```
highhx deps update [OPTIONS] [PACKAGES]...
```

| Option | Description |
|---|---|
| `--manager, -m` | Package manager to use. |

## Testing & build

### `highhx test`

Discover the test framework (pytest, unittest, Jest, Vitest, npm test,
flutter test, Maven, Gradle, ctest, go test, cargo test) and run it.

Extra ARGS are passed to the test runner (put them after `--`).
Exit code mirrors the test runner's.

```
highhx test [OPTIONS] [ARGS]...
```

| Option | Description |
|---|---|
| `--watch` | Re-run tests when files change. |
| `--coverage` | Collect coverage. |
| `--changed` | Only tests related to files changed since HEAD (git). |
| `--base` | With --changed: also include changes since REF (e.g. main). |

### `highhx benchmark`

Run COMMAND (default: the test command) repeatedly and report min/mean/median/max.

```
highhx benchmark [OPTIONS] [COMMAND]...
```

| Option | Description |
|---|---|
| `--runs, -n` |  (default: `5`) |
| `--warmup` |  (default: `1`) |

### `highhx build`

Run the build workflow (if present) or the build command, then record
artifacts with SHA-256 checksums.

```
highhx build [OPTIONS]
```

| Option | Description |
|---|---|
| `--workflow` | Use .highhx/workflows/build.yaml when it exists. |

### `highhx clean`

Delete regenerable build output (dist/, build/, target/, __pycache__,
.pytest_cache …) inside the project only. Runs commands.clean if configured.

```
highhx clean [OPTIONS]
```

### `highhx package`

Run commands.package (or the ecosystem default: `python -m build`,
`npm pack`, `mvn package`, `docker build` …) and list the artifacts.

```
highhx package [OPTIONS]
```

### `highhx artifacts`

List artifacts in dist/, build/, target/ … with size and SHA-256.

```
highhx artifacts [OPTIONS]
```

| Option | Description |
|---|---|
| `--verify` | Compare files with the checksums recorded at build time. |

## Environment

### `highhx env`

Inspect and edit .env-based environment profiles. Secret values are never printed.

Running `highhx env` without a subcommand runs `highhx env show`.

#### `highhx env check`

Check that every required variable is set for the profile (values are not shown).

```
highhx env check [OPTIONS]
```

| Option | Description |
|---|---|
| `--profile, -p` | Environment profile (default: the active one). |

#### `highhx env diff`

Show variables that exist only in LEFT or RIGHT, or differ between them.

```
highhx env diff [OPTIONS] LEFT RIGHT
```

#### `highhx env profile`

Without NAME, list profiles. With NAME, make it the active profile
(switching to a protected profile such as production needs confirmation).
HIGHHX_ENV overrides the stored choice.

```
highhx env profile [OPTIONS] [NAME]
```

#### `highhx env set`

Set NAME=VALUE, or just NAME to be prompted without echo (recommended for
secrets so they don't end up in shell history). New .env files are created
with owner-only permissions and added to .gitignore.

```
highhx env set [OPTIONS] ASSIGNMENT
```

| Option | Description |
|---|---|
| `--profile, -p` | Environment profile (default: the active one). |
| `--file` | Write to this .env file instead of the profile's first file. |
| `--stdin` | Read the value from standard input. |
| `--unset` | Remove the variable instead. |

#### `highhx env show`

Show every variable of the active (or given) environment profile with its source file.
Secret values are always masked; only their length is shown.

```
highhx env show [OPTIONS]
```

| Option | Description |
|---|---|
| `--profile, -p` | Environment profile (default: active). |

## Git & releases

### `highhx git`

Git helpers that never perform destructive operations silently: pushes,
force operations and branch deletion always require confirmation.

Running `highhx git` without a subcommand runs `highhx git status`.

#### `highhx git branch`

List branches, or manage branch NAME. Deleting an unmerged branch needs
--force as well; deleting protected branches is critical and always asks.

```
highhx git branch [OPTIONS] [NAME]
```

| Option | Description |
|---|---|
| `--switch, -s` | Switch to NAME (existing branch). |
| `--create, -c` | Create NAME (and switch to it unless --no-switch). |
| `--no-switch` | With --create: stay on the current branch. |
| `--from` | With --create: start point. |
| `--delete, -d` | Delete NAME (asks for confirmation). |

#### `highhx git commit`

Commit staged changes (or PATHS / --all). Refuses to commit files that
policies mark as forbidden (e.g. .env, private keys).

```
highhx git commit [OPTIONS] [PATHS]...
```

| Option | Description |
|---|---|
| `--message, -m` | Commit message. |
| `--all, -a` | Stage all changes, including untracked files. |
| `--conventional` | Require a Conventional Commit message (feat:, fix: …). |
| `--allow-empty` |  |

#### `highhx git diff`

Show the diff of the working tree, staged changes, or against REVISION.

```
highhx git diff [OPTIONS] [REVISION] [PATHS]...
```

| Option | Description |
|---|---|
| `--staged` | Staged changes. |
| `--stat` | Only per-file line counts. |

#### `highhx git history`

Show recent commits (optionally for REVISION and PATHS) with the Conventional
Commit type of each, which is what `highhx changelog` groups by.

```
highhx git history [OPTIONS] [REVISION] [PATHS]...
```

| Option | Description |
|---|---|
| `--limit, -n` |  (default: `20`) |

#### `highhx git status`

Show the current branch, its upstream (ahead/behind), and staged, unstaged,
untracked and conflicted files.

```
highhx git status [OPTIONS]
```

#### `highhx git sync`

Fetch from REMOTE and fast-forward the current branch. Never merges or
rebases; a diverged branch is reported so you can decide what to do.

```
highhx git sync [OPTIONS]
```

| Option | Description |
|---|---|
| `--remote` |  (default: `origin`) |
| `--push` | Push local commits (asks for confirmation). |
| `--force-push` | Force-push with lease (critical; asks you to type 'yes'). |

#### `highhx git tag`

Without NAME, list tags newest first. With NAME, create an annotated tag at HEAD;
--push also pushes it to origin (dangerous: asks for confirmation).

```
highhx git tag [OPTIONS] [NAME]
```

| Option | Description |
|---|---|
| `--message, -m` | Tag message. |
| `--push` | Push the tag to origin (asks for confirmation). |

### `highhx version`

Without an argument, show the current version and the bump suggested by
commits since the last tag. With major/minor/patch/prerelease or an explicit
version, update the version files (no commit — see `highhx release`).

```
highhx version [OPTIONS] [major|minor|patch|prerelease|X.Y.Z]
```

### `highhx changelog`

Group commits since the last version tag by Conventional Commit type.
Only real commits are used — nothing is invented. Prints unless --write.

```
highhx changelog [OPTIONS]
```

| Option | Description |
|---|---|
| `--write` | Insert the section into the changelog file. |
| `--version` | Version heading (default: suggested next version). |

### `highhx release`

Create a release from a clean working tree:

1. choose the version (auto = from Conventional Commits)
2. update version files and the changelog (from real git history)
3. commit `chore(release): vX.Y.Z` and create an annotated tag
4. optionally push (asks for confirmation)

```
highhx release [OPTIONS] [auto|major|minor|patch|prerelease|X.Y.Z]
```

| Option | Description |
|---|---|
| `--push` | Push the release commit and tag (default: release.push). |
| `--allow-dirty` | Allow uncommitted changes (they are not included). |

### `highhx publish`

Publish with release.publish_command or the ecosystem default
(twine/uv/poetry, npm publish, dart pub publish, mvn deploy, cargo publish).
Always requires typing the project name to confirm.

```
highhx publish [OPTIONS]
```

## Deployment

### `highhx deploy`

Deploy with `highhx deploy [TARGET]`; inspect with `deploy status` / `deploy logs`.

Targets are defined under deploy.targets in .highhx/config.yaml (types:
local, docker, ssh, kubernetes, terraform, plugin:<name>).

Running `highhx deploy` without a subcommand runs `highhx deploy to`.

#### `highhx deploy logs`

Stream the target's own logs (logs_command, compose logs, kubectl logs),
or with --id print what HighhX recorded while running that deployment.

```
highhx deploy logs [OPTIONS] [TARGET]
```

| Option | Description |
|---|---|
| `--id` | Show the HighhX log of a recorded deployment. |
| `--follow, -f` |  |
| `--tail` |  (default: `200`) |

#### `highhx deploy status`

Show the latest recorded deployment of each target and query the target's live
status (status_command, Compose, kubectl …). --history lists past deployments.

```
highhx deploy status [OPTIONS] [TARGET]
```

| Option | Description |
|---|---|
| `--history` | Show deployment history instead. |
| `--no-live` | Don't query targets, only recorded state. |

#### `highhx deploy to`

Run preflight checks, ask for approval (production targets require typing
the target name), deploy, run health checks and record the deployment.
Success is only reported when the deployment command and health check pass.

```
highhx deploy to [OPTIONS] [TARGET]
```

| Option | Description |
|---|---|
| `--version` | Version label (default: project version or commit). |
| `--skip-preflight` | Skip preflight checks (not recommended). |

### `highhx rollback`

Restore the last successful deployment with a different version (or --to),
using the target's rollback mechanism, then run its health check.

```
highhx rollback [OPTIONS] [TARGET]
```

| Option | Description |
|---|---|
| `--to` | Specific deployment to restore. |

### `highhx environments`

List the deployment targets defined under deploy.targets, their type, whether they
are production, where they deploy to and the result of the last deployment.

```
highhx environments [OPTIONS]
```

## Security

### `highhx security`

Local-first security checks. Findings are concrete and located; secret
values are never printed or logged.

Running `highhx security` without a subcommand runs `highhx security scan`.

#### `highhx security config`

Check HighhX and project configuration for unsafe settings, loose file permissions
(.env, private keys, hooks) and dangerous workflow steps without `approval`.

```
highhx security config [OPTIONS]
```

| Option | Description |
|---|---|
| `--fail-on` | Exit with code 9 if a finding at or above this severity exists. (default: `high`) |

#### `highhx security deps`

Report known vulnerabilities in dependencies using the ecosystem's local audit tool
(pip-audit, npm/pnpm/yarn audit, cargo audit). Ecosystems without an installed tool are listed as skipped.

```
highhx security deps [OPTIONS]
```

| Option | Description |
|---|---|
| `--fail-on` | Exit with code 9 if a finding at or above this severity exists. (default: `high`) |

#### `highhx security report`

Run every check and write the complete report as Markdown or JSON to --output
(or print it). Overwriting an existing file asks for confirmation unless --force.

```
highhx security report [OPTIONS]
```

| Option | Description |
|---|---|
| `--format` |  (default: `markdown`) |
| `--output, -o` | File to write (default: print). |

#### `highhx security scan`

Run the local security checks (secrets, permissions, config, policy, workflows,
dependencies). By default only committed files are scanned; --all-files includes
untracked ones. Exits with code 9 when a finding reaches --fail-on.

```
highhx security scan [OPTIONS]
```

| Option | Description |
|---|---|
| `--check` | Only these checks. |
| `--all-files` | Scan untracked files too (default: committed files). |
| `--fail-on` | Exit with code 9 if a finding at or above this severity exists. (default: `high`) |

#### `highhx security secrets`

Scan committed files for credentials (cloud keys, tokens, private keys, passwords in
URLs …) and for files forbidden by policy. Reports file, line and type only — never the value.

```
highhx security secrets [OPTIONS]
```

| Option | Description |
|---|---|
| `--all-files` | Include untracked files. |
| `--fail-on` | Exit with code 9 if a finding at or above this severity exists. (default: `high`) |

## Containers, services & data

### `highhx docker`

Manage the project's Docker Compose stack through a safe wrapper.

Running `highhx docker` without a subcommand runs `highhx docker ps`.

#### `highhx docker down`

Stop and remove the project's Compose containers. --volumes also deletes named
volumes and their data, which is critical risk and needs typed confirmation.

```
highhx docker down [OPTIONS]
```

| Option | Description |
|---|---|
| `--volumes` | Also DELETE named volumes (critical; asks to confirm). |
| `--remove-orphans` |  |

#### `highhx docker logs`

Print the logs of the Compose services (all, or SERVICES); --follow streams until Ctrl+C.

```
highhx docker logs [OPTIONS] [SERVICES]...
```

| Option | Description |
|---|---|
| `--follow, -f` |  |
| `--tail` |  (default: `200`) |

#### `highhx docker ps`

Show whether Docker is installed and running, which Compose file is used, and the
state, health and ports of each Compose service.

```
highhx docker ps [OPTIONS]
```

#### `highhx docker up`

Start the project's Compose services (all, or SERVICES) in the background;
--build rebuilds images first, --attach runs in the foreground.

```
highhx docker up [OPTIONS] [SERVICES]...
```

| Option | Description |
|---|---|
| `--build` | Build images first. |
| `--attach` | Run in the foreground instead of detached. |

### `highhx services`

Show HighhX-managed services (from config) and Docker Compose services.

```
highhx services [OPTIONS]
```

### `highhx ports`

Check PORTS, or every port configured for services.

```
highhx ports [OPTIONS] [PORTS]...
```

### `highhx db`

Database operations for PostgreSQL, MySQL and SQLite via adapters.
The connection URL comes from the environment (database.url_env, default DATABASE_URL).

Running `highhx db` without a subcommand runs `highhx db status`.

#### `highhx db backup`

Write a backup of the database to database.backups_dir (SQLite: online backup;
PostgreSQL: pg_dump -Fc; MySQL: mysqldump). --list shows existing backups.

```
highhx db backup [OPTIONS]
```

| Option | Description |
|---|---|
| `--list` | List existing backups. |

#### `highhx db migrate`

Run database.migrations.command, or apply pending NNN_name.sql files from
migrations/ in order, each in a transaction, recording checksums.

```
highhx db migrate [OPTIONS]
```

#### `highhx db restore`

Restore BACKUP (file name or path; default: newest). Current data is
overwritten, so this always requires typed confirmation.

```
highhx db restore [OPTIONS] [BACKUP]
```

| Option | Description |
|---|---|
| `--no-safety-backup` | Skip the automatic backup of current data. |

#### `highhx db seed`

Load seed data with database.seed.command or the SQL files in seeds/.
Dangerous (critical on protected profiles), so it asks for confirmation.

```
highhx db seed [OPTIONS]
```

#### `highhx db status`

Check that the database from the active environment profile is reachable and show
which migrations are applied, pending, or were modified after being applied.

```
highhx db status [OPTIONS]
```

## Workflows & automation

### `highhx workflow`

Workflows are YAML files in .highhx/workflows (see docs/AUTOMATION.md): commands,
HighhX actions and other workflows as steps, with dependencies, conditions, retries,
approvals and rollback. Run one with `highhx workflow run <name>` (or `highhx run`).

Running `highhx workflow` without a subcommand runs `highhx workflow list`.

#### `highhx workflow cancel`

Interrupt the process running EXECUTION_ID. The run stops its commands, is recorded as
cancelled and can be resumed later.

```
highhx workflow cancel [OPTIONS] EXECUTION_ID
```

#### `highhx workflow create`

Create .highhx/workflows/NAME.yaml, filled with this project's detected commands.

```
highhx workflow create [OPTIONS] NAME
```

| Option | Description |
|---|---|
| `--template, -t` | Template (dev, test, build, ci, release, deploy, rollback …). Default: blank. |
| `--list-templates` | Show available templates. |

#### `highhx workflow graph`

Print execution stages (steps in the same stage run in parallel), or
Graphviz DOT / Mermaid source for diagrams.

```
highhx workflow graph [OPTIONS] NAME
```

| Option | Description |
|---|---|
| `--format` |  (default: `text`) |

#### `highhx workflow inspect`

TARGET is a workflow name (shows its steps: kind, dependencies, conditions, approvals,
retries and rollback) or a run id (shows each step's status, exit code and error).

```
highhx workflow inspect [OPTIONS] TARGET
```

#### `highhx workflow list`

List the workflows in .highhx/workflows (and those contributed by plugins) with
their description, event triggers and source.

```
highhx workflow list [OPTIONS]
```

#### `highhx workflow resume`

Run the workflow of EXECUTION_ID again with the same inputs, reusing every step that
already succeeded (and its outputs); the rest runs as usual, with approvals. Extra
environment variables given to the first run are not stored — pass them again with -e.

```
highhx workflow resume [OPTIONS] EXECUTION_ID
```

| Option | Description |
|---|---|
| `--env, -e` | Environment for the resumed run (not stored). |

#### `highhx workflow run`

Run workflow NAME. Every step is classified and approved as it runs; with
`on_failure: rollback` completed steps are undone when a later step fails.
A failed or interrupted run can be resumed with `highhx workflow resume ID`.

```
highhx workflow run [OPTIONS] NAME
```

| Option | Description |
|---|---|
| `--input, -i` | Workflow input (repeatable). |
| `--env, -e` | Extra environment variable (repeatable). |

#### `highhx workflow runs`

Workflow executions, newest first: status, duration, failed steps. Resume a failed run
with `highhx workflow resume ID`, cancel a running one with `highhx workflow cancel ID`.

```
highhx workflow runs [OPTIONS]
```

| Option | Description |
|---|---|
| `--running` | Only runs in progress right now. |
| `--limit` |  (default: `20`) |

#### `highhx workflow validate`

Check schema, duplicate ids, missing/circular dependencies, conditions,
variable references, reusable workflows, unparsable commands and risky
commands without approval. Validates every workflow when no NAMES are given.

```
highhx workflow validate [OPTIONS] [NAMES]...
```

| Option | Description |
|---|---|
| `--strict` | Treat warnings as errors. |
| `--no-tool-check` | Don't warn about commands missing from PATH. |

### `highhx actions`

Every capability HighhX executes is an action with an input schema, a risk level,
required permissions, a timeout, a retry policy (idempotent actions only), verification
and — where possible — a compensation for rollback. Plain-language requests, workflow
steps and the AI agent all run actions through the same executor and approvals.

Running `highhx actions` without a subcommand runs `highhx actions list`.

#### `highhx actions list`

List the actions (optionally one CATEGORY: project, filesystem, git, package, docker,
database, service, browser, computer, deployment, security, workflow, shell) with their
risk floor, retries (idempotent actions only) and whether they can be undone.

```
highhx actions list [OPTIONS] [CATEGORY]
```

#### `highhx actions plan`

Validate NAME with INPUTS (key=value …) and rate it — the classifier's verdict on the
concrete action, the catalog's floor and the approval rule — without running anything.

```
highhx actions plan [OPTIONS] NAME [INPUTS]...
```

| Option | Description |
|---|---|
| `--with-json` | Inputs as one JSON object. |

#### `highhx actions run`

Run NAME with INPUTS (key=value …). Medium and high risk actions ask (or run with
--yes); critical ones need a typed confirmation in a terminal; blocked ones never run.

```
highhx actions run [OPTIONS] NAME [INPUTS]...
```

| Option | Description |
|---|---|
| `--with-json` | Inputs as one JSON object. |

#### `highhx actions show`

Show NAME's contract: input schema, outputs, risk floor, kind, permissions, timeout,
retry policy, verification, compensation and the plan feature the AI agent needs.

```
highhx actions show [OPTIONS] NAME
```

### `highhx schedule`

Schedules are defined under `schedules:` in .highhx/config.yaml (5-field
cron syntax). `highhx schedule run` keeps running and executes them on time;
no system cron or background service is installed.

Running `highhx schedule` without a subcommand runs `highhx schedule list`.

#### `highhx schedule list`

Show each schedule from `schedules:` with its cron expression, what it runs, when it
last ran and when it will run next (UTC).

```
highhx schedule list [OPTIONS]
```

#### `highhx schedule run`

Keep running in the foreground and execute schedules when they are due.
--once runs whatever is due right now and exits (useful from system cron / CI).

```
highhx schedule run [OPTIONS]
```

| Option | Description |
|---|---|
| `--once` | Run due schedules once and exit. |

#### `highhx schedule trigger`

Run the schedule NAME immediately, regardless of its cron expression.

```
highhx schedule trigger [OPTIONS] NAME
```

### `highhx hook`

Map git hooks to workflows/commands with `hooks:` in .highhx/config.yaml
(e.g. `pre-commit: test`) and install them with `highhx hook install`.

Running `highhx hook` without a subcommand runs `highhx hook list`.

#### `highhx hook install`

Install git hooks (NAMES, or every hook in `hooks:`) that call `highhx hook run`.
An existing hook not created by HighhX is only replaced with --force (it is kept as .bak).

```
highhx hook install [OPTIONS] [NAMES]...
```

#### `highhx hook list`

Show git hooks that are configured in `hooks:`, installed in .git/hooks (and whether
HighhX manages them), or have scripts in .highhx/hooks.

```
highhx hook list [OPTIONS]
```

#### `highhx hook run`

Run what is configured for git hook NAME — the workflow or command in `hooks:` and
any .highhx/hooks/NAME* scripts. Git calls this; a non-zero exit aborts the git operation.

```
highhx hook run [OPTIONS] {pre-commit|prepare-commit-msg|commit-msg|post-commit|pre-push|post-checkout|post-merge|pre-rebase} [ARGS]...
```

#### `highhx hook uninstall`

Remove HighhX-managed git hooks (NAMES, or all) and restore any hook they replaced.

```
highhx hook uninstall [OPTIONS] [NAMES]...
```

### `highhx trigger`

Run every workflow listening for EVENT (via `on:` in the workflow or
`triggers:` in config). Without EVENT, list known events.

```
highhx trigger [OPTIONS] [EVENT]
```

### `highhx watchers`

List the file watchers configured under `watch:`. Start them with `highhx watch`.

```
highhx watchers [OPTIONS]
```

## Observability

### `highhx logs`

Logs of EXECUTION_ID (default: the most recent execution). Secrets are
redacted when logs are written.

```
highhx logs [OPTIONS] [EXECUTION_ID]
```

| Option | Description |
|---|---|
| `--follow, -f` | Keep printing new lines until the execution finishes. |
| `--tail, -n` | Only the last N lines. |
| `--service` | Show a background service's log instead. |

### `highhx history`

Without EXECUTION_ID, list recent executions (filter with --kind/--status). With an
id (or unique prefix), show its details and steps.

```
highhx history [OPTIONS] [EXECUTION_ID]
```

| Option | Description |
|---|---|
| `--limit, -n` |  (default: `20`) |
| `--kind` | Filter: command, workflow, deploy, release … |
| `--status` |  |

### `highhx events`

The most recent events, oldest first. Events are stored as JSON Lines (secrets
redacted) under the HighhX data directory, one file per day.

```
highhx events [OPTIONS]
```

| Option | Description |
|---|---|
| `--session` | Only this interactive session. |
| `--type` | Only events whose name starts with this (e.g. action.). |
| `--limit, -n` |  (default: `50`) |

### `highhx audit`

Every action HighhX automation classified and decided on — by the agent or by
`highhx computer` / `highhx do` — with its risk, decision (allowed, confirmed,
denied, blocked), outcome and verification. Values are redacted.

```
highhx audit [OPTIONS]
```

| Option | Description |
|---|---|
| `--limit` | Number of entries. (default: `30`) |
| `--session` | Only entries from one agent session. |

### `highhx report`

Summarise execution history for the last --days: runs, success rate, and median and
p95 durations per command and workflow.

```
highhx report [OPTIONS]
```

| Option | Description |
|---|---|
| `--days` |  (default: `30`) |

### `highhx trace`

Show where time went in EXECUTION_ID (default: latest).

```
highhx trace [OPTIONS] [EXECUTION_ID]
```

### `highhx runs`

Every plain-language request HighhX handled locally: the deterministic decision, the
JSON plan, each step's result and verification (`highhx runs show`), and
totals (`highhx runs stats`).

Running `highhx runs` without a subcommand runs `highhx runs list`.

#### `highhx runs list`

The most recent runs: request, route (local, unknown, pro), status and verification.

```
highhx runs list [OPTIONS]
```

| Option | Description |
|---|---|
| `--limit, -n` |  (default: `20`) |

#### `highhx runs show`

Show RUN_ID (default: the latest run).

```
highhx runs show [OPTIONS] [RUN_ID]
```

#### `highhx runs stats`

Totals over recent runs: successes and failures, average time, action and verification
failures, Pro escalations, and the most-used actions and targets.

```
highhx runs stats [OPTIONS]
```

| Option | Description |
|---|---|
| `--limit` | Runs to include. (default: `1000`) |

## Extensibility & team

### `highhx plugin`

Plugins add commands, workflows, templates, detectors and deployment
backends. Code plugins only run when plugins.allow_code is true and their
files match the hash recorded at installation (see docs/plugins.md).

Running `highhx plugin` without a subcommand runs `highhx plugin list`.

#### `highhx plugin install`

Validate the manifest, show its permissions and whether it contains code,
ask for approval, copy it and record its SHA-256 in the plugins lock file.

```
highhx plugin install [OPTIONS] SOURCE
```

| Option | Description |
|---|---|
| `--global` | Install for your user instead of this project. |

#### `highhx plugin list`

List installed plugins (project and user), their status, whether their code is loaded,
what they contribute and why code may be blocked.

```
highhx plugin list [OPTIONS]
```

#### `highhx plugin remove`

Uninstall plugin NAME from the project (or --global for your user).

```
highhx plugin remove [OPTIONS] NAME
```

| Option | Description |
|---|---|
| `--global` |  |

#### `highhx plugin search`

Search plugins listed in plugins.index (JSON index files or directories
of plugins). Works offline; nothing is fetched from the network.

```
highhx plugin search [OPTIONS] [QUERY]
```

#### `highhx plugin trust`

Record that you reviewed and trust the current files of plugin NAME.

Needed for code plugins that arrive with a repository (for example a teammate
committed .highhx/plugins/NAME) or after a plugin's files changed. Trust is
stored per user and per exact file contents (SHA-256); any later change
blocks the code again until you trust it anew. Code still only runs when
plugins.allow_code is true.

```
highhx plugin trust [OPTIONS] NAME
```

| Option | Description |
|---|---|
| `--global` | The plugin is installed for your user. |

#### `highhx plugin update`

Reinstall plugin NAME from the source recorded when it was installed, showing its
permissions again before replacing it.

```
highhx plugin update [OPTIONS] NAME
```

| Option | Description |
|---|---|
| `--global` |  |

### `highhx config`

Project configuration lives in .highhx/config.yaml (see docs/configuration.md).

Running `highhx config` without a subcommand runs `highhx config show`.

#### `highhx config get`

Print the configuration value at a dotted KEY such as commands.test or deploy.default.

```
highhx config get [OPTIONS] KEY
```

#### `highhx config path`

Print the path of .highhx/config.yaml (exit code 4 if it does not exist).

```
highhx config path [OPTIONS]
```

#### `highhx config schema`

Print the JSON Schema for config.yaml, workflow files or plugin manifests, for editor
validation and autocompletion.

```
highhx config schema [OPTIONS] [config|workflow|plugin]
```

#### `highhx config show`

Print the effective configuration (config.yaml merged with --config-profile) and the
files it came from.

```
highhx config show [OPTIONS]
```

#### `highhx config validate`

Validate every configuration file and report all problems at once.

```
highhx config validate [OPTIONS]
```

### `highhx policy`

Policies live in .highhx/policies.yaml. Rules can allow, warn, require
approval (optionally non-bypassable) or deny actions and commands.

Running `highhx policy` without a subcommand runs `highhx policy show`.

#### `highhx policy check`

Evaluate the policies against the repository now: forbidden files that are committed,
whether the branch is protected, and clean-tree requirements. Exits 7 on violations.

```
highhx policy check [OPTIONS]
```

#### `highhx policy show`

Show protected branches, clean-tree requirements, forbidden files, release branches and
every policy rule with its effect.

```
highhx policy show [OPTIONS]
```

#### `highhx policy validate`

Validate .highhx/policies.yaml and report every problem (exit code 3 when invalid).

```
highhx policy validate [OPTIONS]
```

### `highhx workspace`

Workspace members come from `workspace.members` in config or are detected
(pnpm/npm/yarn workspaces, uv, Cargo, Go, Maven, Gradle, apps/* + packages/*).

Running `highhx workspace` without a subcommand runs `highhx workspace list`.

#### `highhx workspace list`

List workspace members (from workspace.members or detected monorepo tooling) and the
stack detected in each.

```
highhx workspace list [OPTIONS]
```

#### `highhx workspace run`

Run COMMAND in every workspace member, one after another, or with --parallel. Stops at
the first failure unless --continue-on-error.

```
highhx workspace run [OPTIONS] COMMAND...
```

| Option | Description |
|---|---|
| `--parallel, -p` | Run members in parallel. |
| `--max-parallel` |  (default: `4`) |
| `--continue-on-error` | Keep going when a member fails. |

### `highhx profile`

Config profiles are YAML overlays in .highhx/profiles/<name>.yaml applied
with --config-profile NAME or HIGHHX_PROFILE. For environment-variable profiles
(development/staging/production) see `highhx env profile`.

Running `highhx profile` without a subcommand runs `highhx profile list`.

#### `highhx profile create`

Create .highhx/profiles/NAME.yaml, an overlay merged over config.yaml when you pass
--config-profile NAME or set HIGHHX_PROFILE.

```
highhx profile create [OPTIONS] NAME
```

#### `highhx profile list`

List config profiles in .highhx/profiles and mark the one selected with --config-profile.

```
highhx profile list [OPTIONS]
```

#### `highhx profile show`

Show the configuration with profile NAME applied and validate the result.

```
highhx profile show [OPTIONS] NAME
```

## Diagnostics

### `highhx doctor`

Check the OS, Python, Git, Docker, Node, Flutter, Java, package managers,
runtime versions, project configuration, workflows, dependencies,
environment variables, permissions and service ports.

```
highhx doctor [OPTIONS]
```

### `highhx diagnose`

Find concrete failures: invalid config or workflows, tools missing for
configured commands, missing required variables, port conflicts, stale
state, insecure .env permissions and recent failed runs.

```
highhx diagnose [OPTIONS]
```

### `highhx repair`

Only well-understood fixes are automated (create missing directories,
git-ignore state, clear stale state, restrict .env permissions, restore
default workflows). Anything beyond SAFE risk asks for approval.

```
highhx repair [OPTIONS]
```

### `highhx debug`

Versions, paths, detection results and HIGHHX_* variables — secrets are
masked, so the output is safe to paste into an issue.

```
highhx debug [OPTIONS]
```
