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
| Agent loops | `agent/session.py` (Pro chat agent), `goals/loop.py` (browser Task IR loop), `computer/operator/vision.py` (vision loop) | `agent/loop/`: a surface-neutral Planner → Worker → Observer → Verifier → Reflector loop with `RecoveryManager`, checkpoints and resume, tool routing and optional specialists. It drives catalog actions only |
| Verification | `spec.verify`, `verification/strategies.py`, `computer/verify.py` (window predicates) | `verification/declarative.py`: exit code, file, DOM, text, accessibility, URL, screenshot / visual, process, application, network, custom; `any` / `all`; satisfied · unsatisfied · unknown |
| Memory | `agent/history.py` (chat transcripts), `goals/log.py` | `trajectories/` `TrajectoryStore` (task → observation → action → result → verification → reflection), search, summary, replay |
| Benchmarks | `tests/computer_use` (simulated desktop and evaluators, test-only) | `benchmarks/` (task format, environments including the simulated desktop moved from the tests, runner, metrics, report, compare) |
| TUI | `agent/ui.py`, `ui/` | `ui/live.py`: a dashboard that only consumes events |
| Traces | `observability/tracing.py` (spans per execution), `observability/runs.py` | `observability/tasktrace.py`: Task → Plan → Action → (Observation, Grounding, Execution, Verification) → Recovery → Result, built from events, redacted |

## Package map (new code)

```text
src/highhx/
  actions/protocol.py            ActionRequest · ActionOutcome · aliases · submit()
  actions/handlers/android.py    android.* handlers (call drivers/android)
  actions/handlers/sandbox.py    sandbox.* handlers (call runtimes/sandbox)
  actions/handlers/state.py      computer.state (perception through the executor)
  perception/                    ComputerState, providers, StateFusion, VisualDiff, ElementTracker, png
  grounding/                     selectors, grounders, HybridGrounder
  models/                        VisionModel · LanguageModel · EmbeddingModel · OCRModel (+ adapters)
  drivers/                       ComputerDriver protocol, desktop/browser/remote/vm adapters, android/
  runtimes/                      Runtime, LocalRuntime, SandboxRuntime (+ backends), RemoteRuntime
  agent/loop/                    planner, worker, observer, verifier, reflector, recovery,
                                 checkpoint, routing, specialists, loop
  trajectories/                  Trajectory model and TrajectoryStore
  verification/declarative.py    declarative verifiers
  benchmarks/                    format, environments, runner, metrics, report, suites/
  observability/stream.py        trace context and event recorder
  observability/tasktrace.py     task trace trees
  computer/recorder.py           browser workflow recording, replay and healing
  ui/live.py                     event-driven dashboard
  commands/…                     browser, android, sandbox, benchmark (group), trace (group),
                                 trajectories, agent loop, computer state / ground
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

## Free / Pro

| Capability | Tier |
|---|---|
| Action protocol, executor, events, traces, trajectories, declarative verification | Free |
| Perception (DOM, accessibility, OCR), hybrid grounding without vision | Free |
| Browser recording, replay, healing (DOM / accessibility / text / OCR) | Free |
| Android actions, devices, observation (deterministic) | Free · needs `adb` |
| Sandbox create / exec / patch / destroy | Free · LOCAL · needs a backend (Seatbelt on macOS, bubblewrap or Docker on Linux) |
| Benchmarks with scripted planners | Free |
| Vision grounding | Pro (HighhX gateway) or a LOCAL vision model (OPTIONAL) |
| Model planner for `agent loop` / `android agent`; specialists | Pro, or a local model (OPTIONAL) |
| Remote runtime (SSH computer) | Free · REMOTE · OPTIONAL |
| VM and cloud runtimes | not implemented. They report a capability error |

## Migration order

1. action protocol 2. computer state 3. event bus 4. perception 5. grounding 6. agent loop
7. browser upgrades 8. self-healing 9. drivers 10. Android 11. sandbox runtime 12. trajectories
13. benchmarks 14. multi-agent 15. TUI 16. documentation. Each step runs the full suite and is
committed on its own.
