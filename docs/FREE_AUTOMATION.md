# HighhX Free: deterministic computer automation

HighhX Free turns short plain-language requests into verified computer and project automation
**without an LLM, an AI API or an account**:

```text
$ highhx "open Gmail and search internship"
$ highhx "play lofi on YouTube"
$ highhx "open my project and run the tests"
$ highhx                                  # or type them at the ❯ prompt
```

```
natural language ─► deterministic resolver ─► JSON action plan ─► action executor
                     parser · grammar · targets       versioned, logged     risk · approval · guardrails
                                                                            │
      verification ◄── automation bridge (C#/.NET engine or Python engine) ◄┤ desktop
      run trace    ◄── HighhX browser (Chrome via DevTools)                ◄┤ web
                   ◄── file system · shell · git · project toolchain       ◄┘ project
```

HighhX Pro keeps all of this and adds **JEv / advanced reasoning** for open-ended work — see
[Free and Pro](#free-and-pro) below.

## What Free understands

| | Examples |
|---|---|
| Websites | `open Gmail` · `open YouTube` · `open github.com` · `open google drive` · `open my calendar` · `go to localhost 3000` |
| Search | `search YouTube for lofi` · `open Gmail and search internship` · `open Google and search for Python jobs` · `google python decorators` |
| Media | `play lofi on YouTube` · `open YouTube and play Adhento Gani` |
| Applications | `open Chrome` · `open Safari` · `open Terminal` · `open VS Code` · `open Slack` · `switch to Slack` |
| Keyboard & UI | `press cmd+t` · `press enter` · `scroll down` · `click the Save button` · `type "hello" into search` |
| Project & files | `open my project` · `open the readme` · `open hello.py` · `show my files` · `list files in src` · `create a folder called test` · `create a file called hello.py` |
| Developer | `run the tests` · `show git status` · `check git changes` · `run python hello.py` · `deploy staging` · `stop the backend` |
| Workflows | any of the above joined by *and*, *then*, *and then*, *after that*, `,` — e.g. `open YouTube, then play lofi and after that press space` |
| Files & context | `find my PDF` · `show me the latest report` · `find the architecture document from yesterday and open it` · `open my repository on GitHub` · `open it` · `the second one` · `no, I meant GitHub` — see [UNDERSTANDING.md](UNDERSTANDING.md) |

Anything else gets a clear answer instead of a guess:

- **Unknown entity** — `open spotifyy` → *I don't know an app or website called 'spotifyy'
  yet.* (with suggestions). Nothing runs.
- **Guardrail** — `create a file called hello.py` when it exists → *HighhX never overwrites
  it*; `run ls -la` → *shell commands run through `!ls -la`*. Nothing runs.
- **Open-ended** — `find the most important email from last week and draft a response`,
  `debug this authentication system and fix all failing tests`, `refactor this project` →
  the HighhX Pro capability panel. Several deterministic steps are **not** a reason to
  escalate: `open Gmail and search internship` stays on Free.

## The deterministic resolver

`highhx.decision.deterministic.DeterministicDecider.decide(request)` returns a `Decision`,
plain data. It is not JEv and uses no model: JEv / advanced reasoning is HighhX Pro only.

| field | meaning |
|---|---|
| `route` | `local` (Free runs the plan) · `unknown` (explained, nothing runs) · `pro` (open-ended) |
| `intent`, `target` | e.g. `workflow`, `gmail` |
| `clauses` | how the request was split (`["open Gmail", "search internship"]`) |
| `entities` | sites, apps, URLs, queries, paths, keys, commands (typed text is counted, never copied) |
| `plan` | the JSON action plan (below) |
| `unknown` / `reason` / `capability` | why there is no plan, and which Pro capability would handle it |

It is built from separate, testable parts:

| module | job |
|---|---|
| `language/parser.py` | normalisation ("please …", punctuation) and clause splitting — a conjunction splits only before a known verb, so *rock and roll* stays one query |
| `language/grammar.py` | the verb registry: `open`, `search`, `google`, `play`, `click`, `type`, `press`, `scroll`, `focus`/`switch to`, `create`, `list`, `run`; each verb has a full-clause pattern and a handler |
| `language/entities.py` | URLs, queries, media words, file names, interpreters, key combinations |
| `language/targets.py` + `language/data/targets.yaml` | the target registry: websites, applications and project places, as data |
| `actions/resolver.py` | the developer rules (tests, git, deploy, services, workflows …) |
| `decision/deterministic.py`, `decision/risk.py` | the decision, and risk classes |
| `plans/planner.py`, `plans/schema.py` | the JSON plan, its schema and validation |

When this grammar does not resolve a request, a second deterministic stage
(`language/understand.py`) resolves references ("open it", "the second one"), corrections,
project files and the project's repository, and turns every request into HXIR — or asks when
something is unclear. See [UNDERSTANDING.md](UNDERSTANDING.md).

No similarity matching, no model, no network: the same request in the same project always
gives the same decision. `highhx do --plan "…"` shows it; add `--json` for the full decision.

## The target registry

Built-in targets live in [`src/highhx/language/data/targets.yaml`](../src/highhx/language/data/targets.yaml);
add your own in `<config dir>/targets.yaml` (same format — `highhx computer status` reports
problems in it):

```yaml
sites:
  - id: jira
    name: Jira
    aliases: [jira, tickets]
    url: https://example.atlassian.net/
    capabilities: [open, search]
    search: https://example.atlassian.net/issues/?jql=text~"{query}"
    results: /issues/            # URL marker once results show (verification)
    signin: [id.atlassian.com]   # "you are not signed in" pages
apps:
  - {id: obsidian, name: Obsidian, aliases: [obsidian, vault]}
project:
  - {id: specs, aliases: [specs, the specs folder], find: [specs, docs/specs]}
```

Built in: Google, YouTube, Gmail, GitHub, Google Drive, Google Calendar, Stack Overflow,
Wikipedia, Python docs, PyPI, npm, MDN, Reddit, Google Maps, Spotify Web; Chrome, Edge,
Brave, Safari, Firefox, Terminal, iTerm, VS Code, Finder, Slack, Spotify, Notes, Xcode,
System Settings and more (plus any app installed under its exact name); project places:
*my project*, *the readme*, *the source folder*, *the tests folder*, *the docs*.

## JSON action plans

```json
{
  "version": "1",
  "request": "open Gmail and search internship",
  "intent": "workflow",
  "target": "gmail",
  "risk": "safe",
  "risk_level": "low",
  "executor": "browser",
  "steps": [
    {"id": "step_1", "action": "open", "target": "gmail", "catalog_action": "browser.open",
     "params": {"url": "https://mail.google.com/"}, "risk": "safe", "risk_level": "low",
     "executor": "browser", "verification": {"type": "page_open"}, "description": "open Gmail"},
    {"id": "step_2", "action": "search", "target": "gmail", "catalog_action": "browser.search",
     "params": {"query": "internship", "site": "Gmail"}, "risk": "safe", "risk_level": "low",
     "executor": "browser", "verification": {"type": "search_results"},
     "description": "search Gmail for 'internship'"}
  ],
  "verification": {"type": "search_results"}
}
```

- **Versioned** (`"version": "1"`), **serialisable**, **deterministic**, independent of the UI.
- **Every step** has an action (primitive), target, parameters, risk, executor and
  verification. `catalog_action` is the executor action that performs it.
- **Not a capability.** A plan cannot bypass policy: each step is re-planned by the action
  executor, which validates the parameters, classifies the risk again (the command
  classifier, policies, protected paths) and asks for approval by the same rules as any
  action. `validate_plan()` rejects plans with unknown actions, invalid parameters, or a
  primitive/executor that does not match the action. The JSON Schema is `PLAN_SCHEMA` in
  `plans/schema.py`.

### Risk

| class | examples | level (executor) |
|---|---|---|
| **safe** | open an app or website, search, list files, git status, scroll | safe / low |
| **controlled** | create a file or folder, run a program or the tests, type text, key combinations | low writes/exec, medium |
| **high** | delete or overwrite files, destructive shell commands, deploy, push | high / critical |

Plan risk comes from the executor's own classification, so a plan never shows a lower risk
than execution applies. Approvals follow the single approval table in `actions/policy.py`:
safe actions run, low ones follow the approval mode (your own requests run), medium and high
ones ask (`--yes` answers for non-critical ones), critical ones need the typed confirmation,
and commands the policy blocks never run. Outside a terminal nobody can be asked, so a step
that needs approval is not run unless `--yes` covers it.

## Execution and verification

The plan runner (`plans/runner.py`) executes steps in order. After each step, the step's
**verification strategy** (`verification/strategies.py`) checks the state the action was
meant to produce:

| strategy | checks |
|---|---|
| `page_open` | the HighhX browser is on the expected site (a sign-in page is reported as such) |
| `search_results` | the site's results page for the query is showing |
| `media_playing` | the opened result's media element is playing |
| `app_running` / `app_frontmost` | the application is running / in front |
| `file_exists` | the file or folder exists afterwards |
| `listing` · `process_exit` · `command_captured` | the folder was read · the command exited 0 · its output was captured |
| `element_clicked` | the UI changed after a click |
| `keys_sent` · `opened_by_os` | not observable — reported as *not verified*, never as verified |

A step that "succeeded" but fails verification **is a failed step**. On the first failure the
run stops, says which step failed and why, and reports the rest as skipped:

```text
◉ open Gmail  browser.open · low
✗ open Gmail — Gmail asks you to sign in. HighhX never signs in for you: sign in once in the
  HighhX browser window (it keeps its own profile), then run the request again.

Stopped at step_1 (open Gmail): …
0/2 steps · failed · 2.4s · highhx runs show run_20260928T101500_3f2a
```

## Browser automation

Web steps run in **the HighhX browser** — a Chrome (or Edge/Brave) of its own, with its own
profile, driven over the DevTools protocol by the existing computer runtime (element-level
safety, re-observation, verification). `open Chrome and search …` uses it rather than
opening your everyday Chrome as well. Safari and Firefox can be *opened* with a URL by the OS
but not operated: those steps are reported as not verified, and `play …` asks for the HighhX
browser. HighhX never signs in for you; sign in once in the HighhX browser window and it
stays signed in.

## Desktop automation and the C#/.NET engine

Desktop steps (launch, switch to, type, keys, scroll, click, inspect) go through the
**automation bridge** (`automation/engine/`): a fixed, versioned protocol — one JSON object
per line — with 14 operations (`status`, `frontmost`, `launch`, `focus`, `running`,
`open_url`, `click`, `type`, `key`, `hotkey`, `scroll`, `wait`, `inspect`, `verify`).
Every argument is validated before it is sent; there is no script, shell or raw-coordinate
operation; keyboard operations are refused while a terminal is frontmost.

Two engines implement it:

- **`highhx-automation`** — the C#/.NET engine in [`engine/dotnet`](../engine/dotnet/README.md):
  the macOS Accessibility API (AXUIElement), CGEvent keyboard/scroll events and NSWorkspace,
  natively. Used when installed (PATH or `<data dir>/engine/`) and the protocol handshake
  succeeds.
- **The Python engine** — System Events / Accessibility through `osascript`, every process
  run through HighhX's command engine. The default, and the fallback.

`HIGHHX_AUTOMATION_ENGINE=python|dotnet|<path>` chooses explicitly; `highhx computer status`
shows the engine in use. Desktop clicks still pass through the computer runtime, so a
*Delete* or *Send* button asks before it is pressed, and a click that changes nothing is
reported as not done. Missing Accessibility permission is an error with the fix (System
Settings → Privacy & Security → Accessibility), never a silent success.

## Guardrails

All of the action engine's guardrails apply (see [SAFETY_MODEL.md](SAFETY_MODEL.md)), plus:

- Keyboard input never goes to a terminal emulator; shell commands go through `!command`
  (classified like any command).
- A sentence is never run as a shell command: `run python hello.py` builds `python3 hello.py`
  from the interpreter table and an existing project file; anything with arguments is
  refused with the `!command` equivalent.
- `create a file` never overwrites; `open hello.py` opens it in an editor, never runs it,
  and refuses executables.
- Unknown apps, sites and files are named and nothing runs; descriptions that need
  understanding ("the admin page", "my latest invoice") go to Pro.

## Tracing and metrics

Every request is traced — executed or not — in the history database:

```text
$ highhx runs                 # recent runs
$ highhx runs show [RUN_ID]   # decision, plan, each step's result and verification
$ highhx runs stats           # totals, success rate, action/verification failures,
                              # Pro escalations, most-used actions and targets
```

A trace holds the run id, request, the deterministic decision, the JSON plan, risk, executor, each step's
status/verification/duration and the failure reason. Requests go through the secret
redactor; typed text is stored as its length only.

## Free and Pro

```text
FREE  text / voice → router → deterministic resolver → plan → action executor ─┐
PRO   text / voice → router → Pro agent (JEv / advanced reasoning)            │
                              → structured actions / computer tools ──────────┴→ automation bridge
                                                                                → C# engine or Python engine
                                                                                → macOS · browser · shell
```

- **Free** runs everything on this page with no model, no JEv and no AI service. The
  deterministic resolver, the C#/.NET engine, browser, macOS and shell actions are all Free.
- **JEv / advanced decision-model reasoning is Pro only.** Session start, the session and one-shot
  requests all ask one gate, `highhx.decision.advanced` (`advanced_reasoning_available`), which only the HighhX platform can
  satisfy (a cached account grants nothing). This repository has no separate JEv service,
  SDK or endpoint: today the Pro capability behind the gate is the HighhX Pro agent (models via
  the HighhX platform gateway). A future JEv implementation belongs behind the same gate.
- **One execution layer.** The Pro agent's actions go through the same action executor, and
  its desktop computer-use tools through the same automation bridge
  (`ComputerSession.automation()` — one bridge per session, shared with Free's actions), so
  the C# engine, the terminal guard, per-element safety and verification apply to both.
- **Voice** is only an input: push-to-talk → local speech-to-text → you confirm the
  transcript → it is handled exactly like typed text by the same router (Free: the
  deterministic resolver; Pro: the agent). There is no separate voice automation.

## Running it

- In the session: type the request at the `❯` prompt. `/plan <request>` previews, `/approve`
  runs the preview.
- One request: `highhx "…"` (same as `highhx agent "…"` and `highhx do …`). In a terminal
  this opens the session with the request as its first message; outside a terminal (scripts,
  CI, `--json`) it runs the plan and exits `0` when every step succeeded and verified, `1`
  otherwise, and `10` for an open-ended request on Free.
- `highhx do --plan --json "…"` prints the decision and plan without running anything.

## Evals

`tests/evals/automation.yaml` holds the deterministic evals — requests with the expected
route, intent, target, risk, executor, actions, parameters, verification and clause split.
`pytest tests/evals` runs them and prints the score per dimension; they must all pass.
