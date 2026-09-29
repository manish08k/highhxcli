# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/) and the
project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Goal tasks: `highhx computer task`.** A goal runs as a loop — observe the page, choose one
  generic action, execute it through the computer runtime (safety policy, confirmation, audit),
  verify the expected result, recover or replan — until the goal's success conditions hold or
  a well-defined failure. Logged as TASK / PLAN / OBSERVE / ACTION / RESULT / VERIFY /
  RECOVERY / FINAL, and to `<data dir>/tasks/<task id>.jsonl`.
- **Task IR**: goals as validated JSON (closed schemas; `--schema` prints them) with generic
  browser primitives, element targets discovered from the page (ids, role and name, attribute
  filters), `expect` conditions, success and failure conditions, `if` branches and bounded
  `repeat` loops. No primitive runs code or commands.
- HighhX Free builds Task IR from the deterministic resolver (site data from the target
  registry, no per-site code); `--ir FILE` runs Task IR for any site. HighhX Pro's AI planner
  writes Task IR for requests nobody programmed and chooses each action from the page's
  accessibility tree; it also takes over when a deterministic plan fails.

- **Understanding requests as data (HXIR).** When the deterministic grammar does not resolve a
  request, a second deterministic stage turns it into HXIR v1 — goal, clauses, entities with
  evidence, constraints, references, ambiguities — with a closed, versioned schema. Only a
  resolved HXIR runs, and only as catalog actions re-validated against the catalog; everything
  that resolved before resolves exactly as before. See `docs/UNDERSTANDING.md`.
- **References and conversation context** in the interactive session: `open it`, `close that
  tab`, `the second one`, `the file I just opened`, `go back there`, `run the tests here`, and
  corrections (`no, I meant GitHub`). A reference resolves only from what the session actually
  did; with several candidates HighhX asks *Which one do you mean?* and the answer completes
  the request. Nothing is guessed.
- **Project files by description**: `find my PDF`, `show me the latest report`, `open the
  architecture document from yesterday` — a new safe action, `filesystem.find` (names and dates
  only, confined to the project, secret files skipped, verified). Files outside the project
  (e.g. Downloads) are reported as outside the confinement policy, not searched.
- **Constraints**: time windows, ordering, ordinals, file kinds, and execution constraints
  (`don't open anything`, `without changing anything`), which are enforced — a contradicting
  plan does not run.
- More phrasings: `can u …`, `… pls`, `take me to …`, `go back to …`, `close that tab`,
  `open my HighhX project`, `open my repository on GitHub`, `start the browser` (asks when
  several browsers are installed).

- **The HighhX Computer Runtime.** Native desktop observation and control on macOS, Windows and
  Linux, through one Python API (`from highhx.computer import HighhXDriver`) and the automation
  protocol version 2: screenshots, applications, windows (list, move, resize), the accessibility
  tree with element bounds, the element at a point, clicks at points (right, double, triple, and
  best-effort background delivery), drags, wheel scrolling at a point, menus, quitting
  applications and the clipboard. Each platform reports per feature what it supports and why not
  (`highhx computer status`). Design informed by Cua (MIT); no Cua code or dependency.
- 13 `computer.*` actions (`observe`, `screenshot`, `windows`, `apps`, `element_at`, `click_at`,
  `move`, `drag`, `menu`, `window`, `quit`, `clipboard_read`, `clipboard_write`) with verification
  where the effect is observable; clicks at points, drags, menus, quitting and the clipboard
  always ask. CLI: `highhx computer screenshot | windows | at | move | drag | menu | window | quit
  | clipboard | protocol`, `click --at/--text`, `scroll --at`.
- HighhX Pro's agent can use them through `computer_act` (e.g. `click_at:640,400`,
  `menu:File > Save`), as the agent, with the same approvals; desktop observations include
  element positions.
- Optional perception: `click --text` finds text by accessibility, then OCR (tesseract).
- The computer-operations contract is exported as `schemas/computer-protocol.json`.

### Changed

- The HighhX browser reports a failed or timed-out page load as `NavigationError` (a kind of
  `IntegrationError`), so callers can tell "the site answered with an error" from "the browser
  connection failed".
- `switch to the browser` no longer resolves to `git checkout browser` (say `switch to branch
  browser` for the branch).
- Clauses joined by `, and` (`…, and open the first one`) are split like `and`.
- The automation protocol is version 2. An installed .NET engine (protocol 1) keeps working;
  newer operations run on the built-in engine. Desktop scrolling now uses real wheel events.
- Desktop automation is no longer refused on Windows and Linux: their backends do what the
  platform allows and say what it does not.
- Ambiguity and missing-information panels in the session say *Which one do you mean?* /
  *I need more information* instead of *I don't know this action yet*.

## [0.6.4] - 2026-09-29

`open <website>` in HighhX Free: the right tab, every time — and the project's own repository page.

### Changed

- **`open <website>` decides which tab to use.** A tab already showing the page is used (the
  working tab is not reloaded, another tab is switched to); a blank tab or one on the same site
  is navigated; a tab showing another site is kept and the page opens in a new tab. Repeated
  opens never duplicate tabs.
- A site typed as its domain (`open youtube.com`, `open docs.python.org`) opens the site's
  registered address; a URL with a scheme (`open https://youtube.com`) is opened as typed.

### Added

- `open github and open my repository` opens the project's repository page: the git remote on
  the same host as the open site (credentials in the remote are never used). Without a matching
  remote it still opens the project folder.
- GitLab and Bitbucket in the built-in site registry.

### Fixed

- `--dry-run` previews of a plan no longer report "verification failed: no page is open": a
  previewed step is shown as not verified, because nothing ran.

## [0.6.3] - 2026-09-28

The HighhX browser rebuilt around one browser-wide connection, a tab registry and a single
recovery policy.

### Fixed

- **"The browser connection was lost after Page.navigate was sent" after closing a tab (0.6.1).**
  HighhX held a DevTools connection *to one tab*; when that tab closed or was replaced, the next
  command failed. HighhX now connects to the browser itself, learns at once when a tab is
  closed, replaced or crashes, and continues in the tab it was working in, the most recently
  used remaining tab, or a new one.
- A cancelled request (Ctrl+C) no longer leaves its tab holding every later command of the
  session until the abandoned load gives up.
- Transient network errors (timeouts, resets, a changed network) are retried; permanent ones
  (unknown address, refused, certificate) are reported with a hint.
- A download is never mistaken for a page: opening a file URL downloads it and says where.

### Added

- Tab management: `browser.new_tab`, `browser.close_tab`, `browser.switch_tab`, `browser.tabs`;
  "open Gmail" switches to a tab already showing it. A clicked link that opens a new tab, or a
  sign-in window, is followed; ads and other popups are not.
- `browser.back`, `browser.forward` (exact history entries), `browser.refresh`, `browser.hover`,
  `browser.double_click`, `browser.drag` (HTML5 and pointer drags), `browser.upload` (project
  files only, asks first) and `browser.download` (followed to completion in the HighhX downloads
  folder) — as actions, flow steps and plain requests ("go back", "close this tab", "download
  the report link" …).
- Every action has a retry class: safe actions are recovered and repeated (bounded, with
  backoff); clicks, typing, keys, uploads, downloads and reloads are never repeated once they
  may have run — HighhX looks at the page and reports what it found.
- Browser recoveries, tab changes, popups, dialog decisions and downloads are recorded in the
  audit trail (`details.browser`).

## [0.6.2] - 2026-09-28

A reliable HighhX browser: it recovers from dropped connections, crashes and stalled pages
without ever repeating an action.

### Fixed

- **A slow or unresponsive site could leave the browser unusable.** While a navigation that
  never commits is pending, Chrome answers nothing on a new DevTools session except
  `Page.stopLoading`, so every later command (and the next `highhx` invocation, e.g. after
  Ctrl+C) failed with "the browser did not answer". HighhX now stops that load when it
  reconnects, and a site that does not respond is reported as such instead of being retried.
- **A lost connection during `open` failed the action.** Opening a URL is idempotent, so it is
  issued again — at most three times, and only after reconnecting and checking where the page
  is (a page that already got there is not requested again). Clicks, typing and key presses
  whose answer was lost are still never repeated.
- A command that never reached the browser (the connection was already gone) is sent once
  more on a new connection instead of failing; a failed send is no longer a raw OS error.
- Reconnections return to the tab HighhX works in, not to whichever tab is listed first.
- A crashed page is detected at once (it used to hang for 30s); its tab is replaced.
- JavaScript dialogs no longer block every command: alerts are acknowledged, and confirms,
  prompts and "leave page?" dialogs are cancelled — nothing is confirmed or discarded.
- Links with `target=_blank` open (clicks carry a user gesture, like a person's click) and
  HighhX continues in that tab; tabs opened by scripts or ads are not followed.
- Enter and other keys reach the page even when the browser window is in the background or
  its address bar has focus.
- A `www.` redirect counts as arriving at the requested page.

### Added

- Network errors come with a plain-language hint (address not found, offline, connection
  refused, certificate problem), and a failed open says when the page was still loading.
- Downloads from the HighhX browser go to its own `downloads` folder in the user state directory.
- Recovery tests: a scripted DevTools browser for connection loss, crashes, dialogs, stalled
  loads, retries and tabs, plus real-Chrome tests (`HIGHHX_TEST_BROWSER=1`).

## [0.6.1] - 2026-09-28

Local voice with whisper.cpp, and a fix for `/voice` commands being treated as speech.

### Fixed

- **`/voice status` (and any line typed while recording) could reach the resolver and show
  the HighhX Pro screen.** `/voice on` started a recording at once; the next line typed —
  e.g. `/voice status` — only ended that recording, and the room noise it captured was
  transcribed and sent on as a request. Now `/voice` / `/voice on` only turn voice on (push-to-talk
  starts with Enter on an empty line), and a line typed during a recording discards the audio
  untranscribed and is handled as typed. `/voice`, `/voice on`, `/voice off`, `/voice status`,
  `/voice setup` and `/voice test` are session commands that never reach the resolver, the AI
  agent or the capability gate.
- `/voice` with no argument always turns voice on (it used to toggle).
- Model downloads work on Python builds without a CA store (python.org's macOS installer):
  the operating system's CA bundle is used; TLS verification stays on.

### Added

- **whisper.cpp as the only speech-to-text engine** — local, free, no account, no Pro.
  First use of `/voice on` offers to install whisper.cpp and an audio recorder (Homebrew on
  macOS; the system package manager and a pinned whisper.cpp v1.9.4 source build on Linux)
  and to download the `base.en` model with a progress bar and SHA-256 verification into the
  user data directory. What setup finds is remembered in `voice.json`; nothing is reinstalled
  or re-downloaded.
- **`highhx voice status | setup | model | test [--file WAV]`** and **`/voice status | setup
  | test`**: readiness (speech-to-text, model, microphone, recorder, replies, push-to-talk),
  setup that asks before installing, model switching (`tiny.en`, `base.en`, `small.en`) and
  a transcription test that runs nothing. `highhx voice …` typed inside the session runs the
  matching `/voice` command.
- Microphone handling: denied access (macOS delivers digital silence) is reported with the
  System Settings path and a retry; recordings stop after 60 s.
- Transcripts whisper.cpp is unsure of (mean token probability below 60 %) are shown for
  confirmation before anything runs; `"confirm": "always"` in `voice.json` confirms every one.
- Voice regression tests with a fake microphone, whisper.cpp, package manager and model
  server (`tests/unit/voice/`), and prerecorded-speech integration tests against the real
  whisper.cpp (`tests/integration/test_voice_whisper.py`, skipped where it is not installed).

### Changed

- The safety pipeline is unchanged: a transcript is handed to the same `handle()` as typed
  text — resolver or agent, action plan, risk, approval, executor, verification, audit. Voice
  adds only the `voice.heard` event (transcript and confidence) and a spoken reply to spoken
  requests (typed requests are no longer spoken).
- The `run python hello.py; rm -rf ~` eval now expects what the resolver has always done —
  refuse it and point to `!command`, where the destructive part is critical and cannot be
  approved — with a test pinning that guardrail.

### Removed

- Vosk support and the `HIGHHX_VOICE_STT` / `HIGHHX_VOSK_MODEL` variables.

### Known limitations

- The default `base.en` model is English-only: non-English names (e.g. Telugu song titles)
  may be misheard. Voice input is not supported on Windows or WSL yet.

## [0.6.0] - 2026-09-28

HighhX Free becomes deterministic computer automation: natural language → deterministic
resolver → JSON action plan → verified automation, with no AI and no account.

### Added

- **Deterministic decisions** (`highhx.decision.deterministic`): route (local / unknown /
  Pro), intent, target, entities, clauses and a plan for every request; never a model.
- **JSON action plans** (`highhx.plans`): versioned schema (`PLAN_SCHEMA`), validation,
  planner and a plan runner that verifies every step and stops safely at the first failure.
- **Language layer** (`highhx.language`): parser, verb grammar, entity extraction and a YAML
  target registry (websites, applications, project places; extensible in `targets.yaml`).
  New: Gmail, Google Drive, Google Calendar; *search X* without "for"; *open my project*,
  *the readme*, *the source folder*; *show my files*; *switch to <app>* launches if needed.
- **Automation bridge** (`highhx.automation.engine`): protocol v1 with 14 validated
  operations, a terminal guard for keyboard input, the built-in Python engine, and the
  **C#/.NET engine** source (`engine/dotnet`, `highhx-automation`) used when installed.
  The C# engine ships as source only: it has not been compiled or run for this release (no
  .NET SDK was available); the Python engine is the default and what the tests exercise.
- **Verification strategies** (`highhx.verification`): page open, search results, media
  playing, app running/frontmost, file exists, exit code, output captured; unobservable
  steps are reported as not verified.
- **Tracing and metrics:** every request is traced (`automation_runs`); `highhx runs`,
  `highhx runs show`, `highhx runs stats`.
- **`highhx "request"`** on the command line — the same as `highhx agent "request"`; on Free
  outside a terminal it runs the deterministic plan and exits. `highhx do --plan [--json]` previews.
- Deterministic evals (`tests/evals/automation.yaml`).
- **JEv / advanced reasoning is HighhX Pro only:** one gate (`highhx.decision.advanced`)
  used by session start, the session and one-shot requests; only the platform grants it.
  No separate JEv service exists yet (the gate is interface-ready) — today the Pro agent
  provides it. Free never loads the agent runtime, a model provider, the gate or a vendor
  SDK on its automation path.
- **One automation bridge for Free and Pro:** `ComputerSession.automation()` is shared by
  Free's actions and the Pro agent's desktop computer-use tools (which previously called
  macOS Accessibility directly), so both reach the C#/.NET engine when it is installed.

### Fixed

- `highhx "show git status"` reported "No such command".
- Heavy pages (ads, embeds) no longer hold browser steps for 30 s: iframes removed while
  loading are forgotten and sub-frames get a short grace once the page is complete.
- Applications in `/System/Applications/Utilities` (Terminal, Activity Monitor) are found.
- `browser.play` can no longer hang the browser connection while a video's `play()` is pending.

## [0.5.0] - 2026-09-27

The action engine: one catalog, one executor, for everything HighhX runs — and autonomous
tasks that HighhX itself verifies.

### Added

- **Autonomous tasks (Pro):** a request that says when it is done ("…and make sure all tests
  pass") runs until HighhX has verified it — checks derived deterministically, run by HighhX
  after every attempt, failures with their real output fed back, bounded attempts, a
  verified/unverified report; `/task`, `highhx agent --verify`, `/retry` for another round.
- **Action engine** (`highhx.actions`): 61 actions (project, filesystem, git, package, docker,
  database, service, browser, computer, deployment, security, workflow, shell) with input
  schemas, outputs, a risk floor, permissions, timeouts, idempotency-aware retries,
  verification and compensation; one executor for typed commands, plain language, workflow
  steps and the AI agent; action graphs with whole-graph validation and rollback.
- **Five risk levels and one approval table** (safe · low · medium · high · critical);
  critical needs a typed confirmation; `rm -rf` and remote-code pipes are critical; the gate
  shows the five-level risk.
- **Deterministic resolver** with entities from the project (services, deploy targets,
  environments, workflows, files, URLs, quoted messages) and all-or-nothing compound requests.
- **Session:** `/run`, `/plan` → `/approve` / `/deny` (approve exactly the previewed plan),
  `/retry`, `/resume`, `/cancel`, `/workflows`, `/workflow …`, `/voice`; `/changes` and
  `/undo` on Free; `!command` runs as the `shell.run` action.
- **Workflows:** `action:` steps, `rollback:` per step and `on_failure: rollback`,
  `highhx workflow run | inspect | runs | resume | cancel`; resume reuses completed steps and
  re-runs rolled-back ones; cancellation across processes.
- **Pro:** `run_actions` — the agent proposes structured action graphs executed by the same
  engine (plan-feature gated, never pre-approved, no UI actions); agent events.
- **Voice:** push-to-talk with local speech-to-text (whisper.cpp, Vosk) and OS speech;
  `highhx voice`, `/voice`; transcripts are confirmed before they run.
- **Observability:** structured, redacted JSON Lines events (`highhx events`), session ids.
- `highhx actions list | show | plan | run`; plugin commands as bounded
  `plugin.<plugin>.<command>` actions; structured project context.
- Documentation: product specification, architecture, action engine, safety model, security,
  Free/Pro, agent runtime, automation, voice, observability, plugin system, CLI reference,
  roadmap, contributing.

### Changed

- The session reads as one product: `/help` grouped by purpose; `/status` is the hub for
  pending plans (`/approve`, `/deny`), the last failure (`/retry`) and running workflows
  (`/cancel`); `/history` is a ✓ ✗ ⊘ timeline; `/changes` shows created/modified/deleted;
  empty states say what to do next; new `/init`, `/doctor` and `/login`; a *Getting started*
  panel on the first run; `highhx init` points to the first task.
- Approval prompts give a plain reason and show exactly what will change: a diff for file
  writes, what a delete removes, source → destination for moves.
- Command-backed actions in `--json` mode print their own output to stderr, so a command
  still emits exactly one JSON document.
- Command execution waits on the process instead of polling every 50 ms: dispatching a
  command through the full safety pipeline went from ~59 ms to ~3 ms.
- File search follows `.gitignore`; deterministic failures are never retried.
- `docs/architecture.md` → `docs/ARCHITECTURE.md`, `docs/security.md` → `docs/SECURITY.md`.

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
