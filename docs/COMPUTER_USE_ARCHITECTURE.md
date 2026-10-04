# Computer-use architecture

HighhX is a computer-use and developer-automation runtime with **one execution path**. This
document describes the target architecture for perception, grounding, the agent loop, drivers,
runtimes, sandboxes, trajectories and benchmarks, and where each one sits on top of the
abstractions HighhX already had. Its design draws on the public architecture of UI-TARS Desktop,
Agent-S, Browser Use, Skyvern, AndroidWorld and Cua. HighhX vendors no code from any of them and
depends on none of them.

```text
USER / AGENT / WORKFLOW / MCP / BENCHMARK
        │
        ▼
Planner / Resolver ─────────── language/ · plans/ · goals/ · agent/loop/planner.py
        │                      (deterministic first; a model only where it is allowed)
        ▼
Action graph ───────────────── actions/protocol.py ActionRequest → actions/executor.py ActionNode
        │
        ▼
Risk classification ────────── safety/classifier.py + catalog floor (actions/policy.py decide)
        ▼
Policy ─────────────────────── policy/engine.py (policies.yaml), ActionGate.authorize
        ▼
Approval ───────────────────── safety/gate.py (tickets bound to the exact action)
        ▼
Executor ───────────────────── actions/executor.py ActionExecutor (the only way anything runs)
        │    handlers ──► drivers/ (ComputerDriver) ──► computer/driver.py, computer/browser.py,
        │                                               drivers/android/, runtimes/sandbox.py
        ▼
Verification ───────────────── spec.verify · verification/declarative.py · computer/verify.py
        ▼
Recovery / reflection ──────── agent/loop/recovery.py · agent/loop/reflector.py
        ▼
Audit / trace ──────────────── safety/audit.py · storage/history · observability/ (events, traces)
```

## The rule

- AI models produce **data**: a plan, a proposed action, a grounding candidate. They never run
  anything.
- Vision models return candidate boxes. Every click still goes through `ActionExecutor`, and
  the click's risk comes from the *semantic target* (the label) as well as the point, so a
  vision result can never lower a risk or skip an approval.
- Every observation that captures the screen (`computer.state`, `android.observe`,
  `computer.screenshot`) is itself a catalog action. Policy can deny it, and it is audited.
- MCP tools, workflows, benchmarks, multi-agent specialists and recorded-workflow replay all
  call `ActionExecutor.run` / `execute`. None of them has an executor of its own.

## What existed and what is new

| Concern | Existing (kept, reused) | New layer |
|---|---|---|
| Action definition | `actions/spec.py` `ActionSpec`, `Planned`, `ActionResult` | `actions/protocol.py` `ActionRequest` (id, intent, target, grounding, verification, retry, parent, trace) mapped onto `ActionSpec` names; namespaced aliases (`desktop.click` → `computer.click_at`, `shell.exec` → `shell.run`) |
| Execution | `ActionExecutor` (validate → classify → policy → approve → run → verify → audit) | Unchanged. `submit()` adds trace context and outcome classification around it |
| Events | `core/events.EventBus`, `actions/events.EventLog` (JSONL) | Trace envelope on every event (trace/session/task/action ids, source), new event names, `observability/stream.EventRecorder` (ring buffer for the TUI) |
| Observation | `computer/model.Observation` / `UIElement` (DOM + AX), driver `observe()` | `perception/state.ComputerState` (immutable fusion of DOM, AX, OCR, screenshot, cursor, windows, device), `perception/fusion.StateFusion`, `VisualDiff`, `ElementTracker` |
| Grounding | `computer/perception/grounding.ground` (AX → OCR, ambiguity is an error, window binding) | `grounding/` selector set + `HybridGrounder` (accessibility → DOM → text → OCR → vision → coordinates) with recorded attempts. The desktop `ground()` stays the click-time check (`still_there`) |
| Vision | `computer/operator/` (UI-TARS-style operator, OpenAI-compatible local models, Pro gateway) | `models/` interfaces (`VisionModel`, `LanguageModel`, `EmbeddingModel`, `OCRModel`) and adapters over the existing `agent/model` providers. `VisionGrounder` reuses `operator/parse.py` |
| Browser | `computer/browser.py` `ChromeBrowser` (CDP, recovery policy), `computer/flows.py` | `computer/recorder.py`: recording, semantic multi-selector workflows, replay with self-healing, `drivers/browser.BrowserDriver` |
| Desktop | `computer/driver.HighhXDriver` (Cua-style API) over the bridge (macOS / Windows / Linux) | `drivers/desktop.DesktopDriver` adapts it to `ComputerDriver`; `RemoteDriver` = the SSH target |
| Android | — | `drivers/android/` (adb client, uiautomator hierarchy, `AndroidDriver`) and `android.*` catalog actions |
| Environments | `automation/engine/remote.py` (SSH computer) | `runtimes/` (`LocalRuntime`, `SandboxRuntime`, `RemoteRuntime`; `VMRuntime` / `CloudRuntime` report "not available") |
| Sandbox | `execution/isolation.isolated_environment` (env scrubbing) | `runtimes/sandbox.py`: workspace copy, macOS Seatbelt / Linux bubblewrap / Docker isolation backends, network policy, rlimits, timeout, process-group cleanup, patch export; `sandbox.*` actions |
| Agent loops | `agent/session.py` (Pro chat agent), `goals/loop.py` (browser Task IR loop), `computer/operator/vision.py` (vision loop) — all kept | `agent/loop/`: a surface-neutral Planner → Worker → Observer → Verifier → Reflector loop with `RecoveryManager`, checkpoints and resume, tool routing and optional specialists. It drives catalog actions only |
| Verification | `spec.verify`, `verification/strategies.py`, `computer/verify.py` (window predicates) | `verification/declarative.py`: exit code, file, DOM, text, accessibility, URL, screenshot / visual, process, application, network, custom; `any` / `all`; satisfied · unsatisfied · unknown |
| Memory | `agent/history.py` (chat transcripts), `goals/log.py` | `trajectories/` `TrajectoryStore` (task → observation → action → result → verification → reflection), search, summary, replay |
| Benchmarks | `tests/computer_use` (simulated desktop and evaluators, test-only) | `benchmarks/` (task format, environments including the simulated desktop moved from the tests, runner, metrics, report, compare) |
| TUI | `agent/ui.py`, `ui/` | `ui/live.py`: a dashboard that only consumes events |
| Traces | `observability/tracing.py` (spans per execution), `observability/runs.py` | `observability/tasktrace.py`: Task → Plan → Action → (Observation, Grounding, Execution, Verification) → Recovery → Result, built from events, redacted |

## Package map

```text
src/highhx/
  actions/protocol.py              ActionRequest · ActionResponse · aliases · prepare/submit
  actions/catalog_computer.py      computer.state, browser.select/scroll/click_at/insert_text, api.request
  actions/catalog_android.py       android.* (+ handlers/android.py)
  actions/catalog_sandbox.py       sandbox.* (+ handlers/sandbox.py)
  actions/handlers/state.py        computer.state: perception as an audited action
  actions/handlers/api.py          api.request
  perception/                      ComputerState, providers, StateFusion, VisualDiff, StateDiff,
                                   ElementTracker, PerceptionEngine, png (stdlib decoder)
  grounding/                       selectors (Target), six grounders, HybridGrounder
  models/                          VisionModel · LanguageModel · EmbeddingModel · OCRModel, adapters,
                                   registry (Free/Pro and consent rules)
  drivers/                         ComputerDriver, desktop (Mac/Windows/Linux/Remote), browser, vm,
                                   android/ (adb client, hierarchy, driver, perception)
  runtimes/                        Runtime, LocalRuntime, SandboxRuntime (+ backends), RemoteRuntime,
                                   VMRuntime/CloudRuntime (capability errors), process
  agent/loop/                      model, planner, worker, observer, verifier, reflector, recovery,
                                   routing, specialists, loop (checkpoints, resume)
  trajectories/                    Trajectory, TrajectoryStore (search, hints, lessons, replay)
  verification/declarative.py      declarative checks: satisfied · unsatisfied · unknown · unsupported
  benchmarks/                      model, runner, store, environments/ (web, desktop, android), suites/
  observability/stream.py          EventRecorder (the stream consumers read)
  observability/tasktrace.py       TaskTrace, TraceStore, TaskTraceRecorder
  computer/recorder.py             browser workflow recording, storage, replay, healing
  computer/mcp.py                  MCP server: desktop and runtime toolsets (through the protocol)
  computer_use.py                  the executor/planner/stores/live view for the CLI and console
  ui/live.py                       DashboardState, render, LiveDashboard, PausingPrompter
  commands/computer_use/           browser, android, sandbox, replay, trajectories, agent loop, tui
```

## Escalation (performance)

Perception and grounding escalate from cheap to expensive and stop at the first confident,
unambiguous answer:

1. deterministic (an exact selector the caller already has)
2. accessibility (role + accessible name)
3. DOM (id, test id, name attribute, tag/type/href)
4. text (visible text in the structured tree)
5. OCR (local tesseract, when installed and a screenshot is allowed)
6. vision (a configured `VisionModel`, only when the earlier levels failed or the task asks for it)
7. coordinates (a recorded point, used only with a matching window/viewport binding)

Observations are cached per state fingerprint, and a screenshot is taken only when a level
that needs one runs.

## Free / Pro and availability

FREE · PRO: plan. LOCAL: runs on this computer. REMOTE: another computer.
OPTIONAL: needs something installed or configured. EXPERIMENTAL: implemented and tested
against simulations or fakes only, not yet run on the real platform in this build.

| Capability | Tier |
|---|---|
| Action protocol, executor integration, events, task traces, trajectories, declarative verification | FREE · LOCAL |
| Perception (DOM, accessibility), hybrid grounding without vision, self-healing replay | FREE · LOCAL |
| OCR | FREE · LOCAL · OPTIONAL (tesseract) |
| Browser recording and replay | FREE · LOCAL · OPTIONAL (a Chromium-family browser). Tested in real Chrome |
| Browser network evidence for verification | FREE · LOCAL. Tested in real Chrome |
| Agent loop with scripted plans or Free's resolver; benchmarks; the console | FREE · LOCAL |
| Agent loop / specialists with a model planner; vision grounding | PRO (HighhX gateway, with consent) or a LOCAL model (OPTIONAL) |
| Android actions and driver | FREE · LOCAL · OPTIONAL (adb) · EXPERIMENTAL (no real device in this build) |
| Sandboxes: Seatbelt | FREE · LOCAL (macOS). Tested against the real sandbox |
| Sandboxes: bubblewrap, Docker | FREE · LOCAL · OPTIONAL · EXPERIMENTAL here (not installed in this build's environment) |
| Desktop on Windows / Linux | existing backends · EXPERIMENTAL (tested against fakes of the OS layer) |
| Remote runtime / driver (SSH) | FREE · REMOTE · OPTIONAL |
| MCP server (desktop and runtime toolsets) | FREE · LOCAL |
| VM and cloud runtimes | not implemented: they report a capability error |

## Migration order

1. action protocol 2. computer state 3. event bus 4. perception 5. grounding 6. agent loop
7. browser upgrades 8. self-healing 9. drivers 10. Android 11. sandbox runtime 12. trajectories
13. benchmarks 14. multi-agent 15. TUI 16. documentation. Each step ran the full suite and was
committed on its own (see `git log` on `feat/computer-use-runtime`).

Detailed pages: [PERCEPTION](PERCEPTION.md) · [GROUNDING](GROUNDING.md) ·
[AGENT_LOOP](AGENT_LOOP.md) · [SELF_HEALING](SELF_HEALING.md) ·
[BROWSER_AUTOMATION](BROWSER_AUTOMATION.md) · [ANDROID](ANDROID.md) ·
[COMPUTER_RUNTIME](COMPUTER_RUNTIME.md) · [SANDBOX](SANDBOX.md) ·
[TRAJECTORIES](TRAJECTORIES.md) · [BENCHMARKS](BENCHMARKS.md) · [EVENTS](EVENTS.md) ·
[TRACES](TRACES.md) · [MCP](MCP.md) · [TUI](TUI.md) · [PROVIDERS](PROVIDERS.md).
