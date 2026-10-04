# Reference audit (October 2026 phase)

This page records one phase of work: HighhX's computer-use runtime was compared, capability by
capability, with six open-source projects, used **only as references** for features,
architecture, testing and UX:

[UI-TARS Desktop](https://github.com/bytedance/UI-TARS-desktop) ·
[Agent S](https://github.com/simular-ai/Agent-S) ·
[Browser Use](https://github.com/browser-use/browser-use) ·
[Skyvern](https://github.com/Skyvern-AI/skyvern) ·
[AndroidWorld](https://github.com/google-research/android_world) ·
[Cua](https://github.com/trycua/cua)

No code was copied, and no project's architecture replaced HighhX's. Every capability added in
this phase is a catalog action (or a part of one) run by the one `ActionExecutor`: risk,
policy, approval, verification, audit, events, trajectory. This is a snapshot of one phase, not
a promise that later versions keep the same feature set.

Legend: **present** (already in HighhX, kept) · **completed** (was partial, finished this phase)
· **new** (added this phase) · **not implemented** (compatible, left for later) ·
**conflicts** (incompatible with HighhX's security model, deliberately not done) ·
**unavailable here** (implemented; the tool or platform is missing on the build machine).

## A. Computer use / GUI agent

| Capability | Reference | HighhX | Notes |
|---|---|---|---|
| Natural-language control | all | present | `highhx do` (deterministic, Free), `highhx agent loop --model` |
| Screenshot visual grounding | UI-TARS, Agent S | present | `VisionGrounder` behind consent; local or Pro models |
| Click, double click, drag, scroll, type, keys | all | present | `computer.*`, `browser.*`, `android.*` |
| Right click | UI-TARS, Agent S | **completed** | drivers had it; the agent loop now has a `right_click` verb (Android: long press) |
| Hotkeys | Agent S, Cua | **completed** | `computer.hotkey` existed; the loop now has a `hotkey` verb (desktop only; the browser presses one key at a time) |
| Coordinate / visual / accessibility grounding | all | present | hybrid: accessibility → DOM → text → relative → OCR → vision → coordinates |
| Hybrid DOM + visual, switching | UI-TARS, Browser Use | present | grounding escalates per target |
| Window / application awareness | Cua, UI-TARS | present | `computer.window(s)`, apps, focus checks at click time |
| Post-action verification | all | present | declarative checks; UNKNOWN is never success |
| Recovery, retry, reflection, re-planning | Agent S | present | bounded `RecoveryManager` |
| Long-horizon tasks, bounded history | Agent S (`max_trajectory_length`) | present | `ModelPlanner(history_limit=8)`, step/time/failure limits, checkpoints and resume |
| Separate planner and grounding models | Agent S | present | `HIGHHX_PLANNER_*` and `HIGHHX_VISION_*` are separate |
| Model capability detection, consent | UI-TARS | present | remote models need `--remote-model` / `--remote-vision` |
| Human-verification challenges | Browser Use, Skyvern (cloud solving) | **new** (detect + hand over) | the loop stops with `needs_user`; solving or evading CAPTCHAs **conflicts** and is not done |
| Behavior best-of-N rollouts | Agent S | not implemented | needs N independent environments; a single real desktop cannot be rolled back |

## B. Browser automation

| Capability | Reference | HighhX | Notes |
|---|---|---|---|
| Navigation, tabs, back/forward/reload | all | present | |
| Page / element / URL waits | Browser Use | present | `browser.wait` |
| Network waits | Browser Use, Playwright | **new** | `browser.wait {network_idle}` and `{request: {url_contains, method, status}}` |
| DOM / visual / hybrid interaction, forms | all | present | |
| Uploads, downloads, screenshots | all | present | |
| Page extraction | Browser Use | present | `browser.extract` |
| JSON-schema extraction | Skyvern, Browser Use | **new** | deterministic: labels, definition lists, tables and lists mapped to the schema; no model, never guessed |
| Page validation | Skyvern | present | declarative checks; workflow `verify` (new) |
| Persistent profile, cookies | Browser Use | present | HighhX's own Chrome profile, never the person's |
| Password managers, TOTP | Skyvern | not implemented | secrets come from the environment (`text_from_env`) or are asked for |
| CAPTCHA | Browser Use, Skyvern | **new** (detection only) | see A |
| Slow pages, network failures, crashes, connection loss | Browser Use | present | single recovery policy; a mid-message timeout bug fixed in the previous phase |
| Selector fallback and healing | Skyvern | present | |
| Recorder, replay, workflows | Skyvern | present | |
| Livestream | Skyvern, UI-TARS | partial | the live dashboard streams events; no viewport video |
| Remote browser | UI-TARS, Browser Use | present | `HIGHHX_BROWSER_ENDPOINT` (ws to loopback, or wss with verified TLS) |
| Headless / headful, capability detection | all | present | `highhx capabilities` (new) |

## C. Network observability

| Capability | HighhX | Notes |
|---|---|---|
| Requests, responses, status codes, failures, redirects | present (previous phase) | sanitized URL only |
| Request timing | **new** | `ms` from DevTools timestamps |
| Redirect evidence | **new** | `redirect: true` on the hop that redirected |
| Action ↔ request correlation | present | the requests that finished while an action ran |
| In trajectories, events | present | `output.network`, `network.observed` |
| In traces | **new** | a `Network` node per step |
| In the TUI | **new** | a `NETWORK` line |
| Privacy | present | never headers, cookies, bodies or query values |

## D. Workflow / RPA (Skyvern blocks)

| Block | HighhX | Notes |
|---|---|---|
| Browser task / action | present | `action: browser.*`, `highhx agent loop` |
| Data extraction | **new** (schema) | `browser.extract {schema}` |
| Validation | **new** | `verify:` on any step |
| Loops | **new** | `for_each:` (list or expression, ≤ 1000 items, `${{ item }}`, `${{ loop.index }}`) |
| File parsing | **new** | `filesystem.parse`: CSV/TSV/JSON/JSONL/YAML/text, PDF (pdftotext) and Office text |
| E-mail | **new** | `email.send`: SMTP, always asked, TLS unless the server is local |
| HTTP request | present | `api.request` |
| Custom code | present | `run:` steps, `sandbox.exec` (classified; sandboxed when asked) |
| State, checkpoints, retries, failure handling | present | outputs, resume, `retry`, `continue_on_error`, rollback |

## E. Agent architecture

Present: planner (scripted / resolver / model), worker, grounding, reflection, verification,
recovery, re-planning, memory (trajectory lessons as data), specialists (`SupervisorAgent`),
tool routing, resume hints, bounded history, human intervention (`ask_user`, secret fields).
New: CAPTCHA as an intervention point. Not implemented: best-of-N.

## F. Memory, trajectories, retrieval

Present (previous phase): storage, step metadata, outcomes, hybrid search (stems, synonyms,
trigrams, host/app/label, outcome-aware), optional embeddings, bounded notes as data. New:
failure categories per step (benchmark diagnostics, from recorded fields).

## G. Benchmarks

The eight metrics are unchanged. **New, beside them (not metrics):** per-action success, step
and action latency (mean, p50, p95, max), attempts per intent, failure categories, grounding
confidence, and the environment's capability report with every result. Not available: model
latency (model calls are not timed per call in trajectories).

## H. Android (AndroidWorld)

| Capability | HighhX | Notes |
|---|---|---|
| adb, devices, emulator detection | present | |
| Launch, home, back, recents, tap, long press, swipe, text, keys | present | |
| Screenshot, UI hierarchy, current app | present | |
| Verification, trajectories | present | |
| Emulator lifecycle | **new** | `android.emulators`, `android.emulator_start` (gRPC 8554), `android.emulator_stop` |
| AndroidWorld task contract | **new** | `AndroidTask` (`initialize_task`, `is_successful`, `tear_down`, `complexity`, `params`), registered by name |
| Real-device benchmarks | **new** | `environment: {kind: android_device, task: …}`; skipped with the reason when no device |
| **Status** | unavailable here | no adb or emulator on the build machine; all of it is tested through simulated adb only |

## I. Sandbox / isolation (Cua)

Present: Seatbelt (validated), bubblewrap and Docker (implemented, not installed here:
experimental), filesystem/network/process isolation, signal restrictions, resource limits,
timeout cleanup, scrubbed environment, credential folders, symlink escapes, capability
detection. Not implemented: virtual machines and isolated desktops (Cua's Lume, Windows
Sandbox): `VMRuntime` reports this honestly.

## J–K. Drivers, remote computers and browsers

Present: one `ComputerDriver` interface; macOS (validated), Windows and Linux backends
(implemented, not validated in this build); background delivery where the platform allows;
remote computers over SSH (keys only, host keys checked); remote browsers over wss. Nothing is
exposed through an unauthenticated tunnel.

## L–N. MCP, events, CLI/TUI

Present: MCP client and server (a fixed allow-list of computer tools, through the executor),
one event bus with the envelope, the live dashboard. New: `highhx capabilities`, the NETWORK
and YOU (waiting for the person) lines in the TUI, network nodes in traces.

## O–P. Recording, files, extraction

Present: browser recording and replay (desktop tasks are replayed from trajectories:
`highhx replay task_…`), uploads, downloads, OCR (tesseract, when installed). New: schema
extraction, file parsing.

## Q. Models

Present: local OpenAI-compatible endpoints (Ollama, llama.cpp, vLLM); HighhX Pro's platform
models (Anthropic, OpenAI and Gemini adapters run by the platform; the CLI never reads a provider key);
vision and grounding models, separate planner and vision models, capability detection,
timeouts, failures that stop the task with a resume hint, consent for remote models. **No real
model was executed in this phase** (none is configured on the build machine).

## R. Security (this phase)

- `browser.extract {url}` navigated while rated as a plain read: the privileged-scheme
  (`file:`, `javascript:`) and sensitive-URL rules never saw the URL. Fixed: it is classified
  exactly like `browser.open` (new `ActionSpec.kind_for`).
- Navigation URLs in audit rows kept query values that did not look like secrets
  (`?q=private-words`); only `token=`-style values were redacted. Fixed: audit rows, their error
  text and the recorded error/summary of a navigation keep parameter names only, while the
  classifier and the approval prompt still see the whole URL.
- `email.send`: high risk (always asked), password only from the environment, TLS required
  unless the server is local, recipients and subject checked for header injection, the body
  logged only as its length.
- Workflow loops: runtime items spliced into a command line are flagged by the validator, and
  the resulting command is still classified and asked (tested with an injected `rm -rf`).
- Schema extraction never reads password, card, one-time-code or hidden fields.
- A poisoned past trajectory reaches the model only as a bounded note labelled as data, and an
  obeying model still needs typed confirmation, or is refused by the task's limits.
