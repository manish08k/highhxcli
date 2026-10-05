# Reference feature matrix (October 2026 implementation phase)

Every meaningful capability found in the six reference projects, against HighhX after this
phase. The references were used for features, architecture ideas, testing and UX only — no code
was copied and no project's architecture was adopted; everything below is one HighhX runtime
(one executor, one policy and approval path, one event bus, one trajectory store).

[UI-TARS Desktop](https://github.com/bytedance/UI-TARS-desktop) (UT) ·
[Agent S](https://github.com/simular-ai/Agent-S) (AS) ·
[Browser Use](https://github.com/browser-use/browser-use) (BU) ·
[Skyvern](https://github.com/Skyvern-AI/skyvern) (SK) ·
[AndroidWorld](https://github.com/google-research/android_world) (AW) ·
[Cua](https://github.com/trycua/cua) (CU)

**Status:** 🟢 GREEN = implemented and validated in this environment (real Chrome, real macOS
Seatbelt, real pdftotext, or a complete automated test of a platform-free feature) ·
🟡 YELLOW = implemented, real-environment validation pending (the platform/tool/model is not on
the build machine; tested through fakes of its command line or API) · 🟠 ORANGE = experimental ·
🔴 RED = not available.

**Before** is the state at commit `a287da9`; **new** marks what this phase added or completed.
The environment had: macOS 27 (arm64), Google Chrome, `sandbox-exec`, `pdftotext`. Not present
(and, by the owner's decision, not installed): adb/Android emulator, Docker/bubblewrap, Lima/Tart,
tesseract, Ollama or any model server/keys.

## Computer use (desktop)

| Ref | Feature | Before | Now | Where | Tests | Real validation | Security | Status |
|---|---|---|---|---|---|---|---|---|
| UT AS CU | move, click, double/right/middle click, mouse down/up, drag, scroll (incl. horizontal), type, key, hotkey, press | present | present | `computer.*` | unit + simulated desktop | macOS opt-in input tests **not run** (need the owner at the keyboard) | label risk floor; never into a terminal | 🟡 |
| UT CU | wait | partial (protocol op) | **new** `computer.wait` | catalog | unit | n/a | bounded 60 s | 🟢 |
| CU | open/close application, focus/switch window, resize | present | present | `computer.launch/quit/focus/window` | unit | pending (as above) | asked by risk | 🟡 |
| CU | minimize / maximize | missing | **new** `computer.window_state` (maximize: all platforms; minimize: macOS Window menu) | handlers/desktop | simulated desktop | pending | low risk | 🟡 |
| UT CU | clipboard, select all, copy, cut, paste, undo | partial (clipboard) | **new** `computer.edit` (cmd/ctrl per OS) | handlers/desktop | unit | pending | paste/cut medium; never into terminals | 🟡 |
| CU | Windows, Linux (X11/AT-SPI) backends | present | present | automation/engine/platforms | unit | not validated (macOS machine) | same protocol | 🟠 |
| CU | Wayland | missing | missing | — | — | — | — | 🔴 |
| AS | screenshot grounding, coordinate grounding, OCR/accessibility/DOM/vision hybrid, confidence, fallback, healing, verification | present | present; OCR confidence **fixed** (was a constant 0.9) | grounding/, perception/ | unit + real Chrome (DOM) | DOM real; OCR/vision pending | vision behind consent | 🟢 DOM · 🟡 OCR/vision |

## Browser

| Ref | Feature | Before | Now | Where | Tests | Real validation | Security | Status |
|---|---|---|---|---|---|---|---|---|
| BU SK | Chromium/CDP, navigation, tabs, back/forward/reload, waits (URL/DOM/text) | present | present | computer/browser.py | unit + real Chrome | ✓ real Chrome | URL schemes classified | 🟢 |
| BU | network idle / specific request waits | new last phase | present | browser.wait | real Chrome | ✓ | sanitized URLs | 🟢 |
| BU SK | click, double, **right click on a control**, hover, scroll, type, key, select, forms, uploads, downloads | right-click by coordinates only | **completed** `browser.right_click` (+ agent verb uses it) | browser, flows, runtime | unit + simulated | ✓ (real Chrome suite) | label risk floor | 🟢 |
| SK BU | page / DOM / accessibility / schema extraction | present | present | browser.extract | real Chrome | ✓ | secret fields never read | 🟢 |
| SK | visual extraction | partial (screenshot + OCR) | partial | computer.state ocr | unit | OCR pending | — | 🟡 |
| BU | crash / websocket / connection recovery, slow pages, network failures | present | present | browser.py | real Chrome | ✓ (incl. kill -9) | unsafe actions never repeated | 🟢 |
| SK | recorder, replay, selector healing, workflows | present | present | recorder, replay | real Chrome | ✓ | secrets become variables | 🟢 |
| BU | browser profiles (create/list/delete/lock, persistence, isolation) | single profile | **new** profiles + leases; graceful quit **fixed** (abrupt stop lost recent cookies) | computer/profiles.py | unit + real Chrome (isolation, persistence across restart) | ✓ | contents never read; delete/import asked | 🟢 |
| BU | profile import / sync | missing | **new** import (copy, consent = high-risk approval); export deliberately not offered | profiles | unit | ✓ (file level) | high risk | 🟢 |
| BU UT | managed / remote browser sessions (start, stop, heartbeat, reconnect, session id, concurrency, cleanup) | remote endpoint only | **new** BrowserSessionManager; remote endpoint with token **fixed** (unreachable before) | computer/browser_sessions.py | unit + real Chrome (2 profiles, crash → reconnect, remote) | ✓ | tokens never stored | 🟢 |
| SK UT | live viewing (viewport stream, frame rate, bandwidth, recovery) | event stream only | **new** CDP screencast + desktop screenshot poller → web console | computer/livestream.py, ui/web.py | unit + real Chrome (frames, recovery after crash) | ✓ browser; desktop pending | loopback + token; frames never stored | 🟢 browser · 🟡 desktop |
| BU SK | CAPTCHA | detection + hand-off | present (never solved) | perception/challenges.py | unit | ✓ (logic) | no evasion | 🟢 |

## Network observability

| Ref | Feature | Status |
|---|---|---|
| BU SK | requests, responses, status, timing, redirects, failures, action correlation, trajectories, traces, TUI, web console; privacy filtering (no headers, cookies, bodies, query values) | 🟢 (real Chrome) |
| — | navigation URLs in audit rows (query values) | 🟢 fixed last phase |

## Android (AndroidWorld)

| Ref | Feature | Before | Now | Status |
|---|---|---|---|---|
| AW | adb/device/emulator discovery, tap, long press, swipe/drag, text, keys, home/back/recents, open/close app, hierarchy, screenshot, packages, verification, trajectories | present | present | 🟡 (simulated adb only) |
| AW | emulator start/stop | new last phase | present | 🟡 |
| AW | emulator reset (wipe + restart), device health (booted, battery, screen, storage) | missing | **new** `android.emulator_reset`, `android.health` | 🟡 |
| AW | task contract (initialize/is_successful/tear_down/params), scoring, real-device benchmarks | new last phase | present | 🟡 |
| AW | real emulator runs | — | **blocked**: no Android SDK on this machine | 🔴 here |

## OCR

| Feature | Before | Now | Status |
|---|---|---|---|
| tesseract OCR, boxes, text, OCR grounding | present | present | 🟡 (stand-in binary; real tesseract not installed) |
| confidence | lost (constant 0.9) | **fixed** (per line, from tesseract) | 🟡 |
| language configuration | missing | **new** `HIGHHX_OCR_LANG`, validated and checked against installed packs | 🟡 |
| OCR verification | via `text` | **new** `ocr_text` check (pixels only; unknown without OCR) | 🟢 (logic) |

## Sandboxes and virtual computers (Cua)

| Feature | Now | Status |
|---|---|---|
| macOS Seatbelt: filesystem, network, signals, CPU, file size, timeout, environment, credentials, symlinks, cleanup | present | 🟢 real Seatbelt |
| Docker (incl. process limit), bubblewrap | present | 🟠 (not installed here) |
| VM lifecycle: create, start, pause, resume, stop, destroy, snapshot, restore, exec, health | **new** (Lima, Tart backends; was "not implemented") | 🟡 (fake CLIs; neither installed) |
| VM desktop (GUI, mouse, keyboard) | **new**: HighhX inside the VM as an ssh:// computer | 🟡 |
| Windows Sandbox, cloud computers | not implemented | 🔴 |

## Remote computers

| Feature | Now | Status |
|---|---|---|
| SSH remote computer (keys only, host keys checked), exec, desktop through HighhX there | present | 🟠 |
| heartbeat, latency, remote capability (HighhX version) | **new** `remote.check` | 🟡 (stand-in ssh) |
| unauthenticated tunnels | never | — |

## Workflows (Skyvern blocks)

| Block | Before | Now | Status |
|---|---|---|---|
| browser, computer, Android, filesystem, HTTP, email, extraction, file parsing | present | present | 🟢 (Android blocks 🟡) |
| if, for_each, retry, approval, verify, subworkflow (`uses`), custom code (`run` / `sandbox.exec`) | present | present | 🟢 |
| while (bounded), choose (if/elif/else), wait (duration / until), human hand-off, set (transform) | missing | **new** | 🟢 |
| artifact, agent, MCP tool | missing | **new** `artifact.save`, `agent.run`, `mcp.call` / `mcp.resources` | 🟢 |

## HTTP and e-mail

| Feature | Now | Status |
|---|---|---|
| GET/POST/PUT/PATCH/DELETE, headers, query, JSON, form, timeout, retries (idempotent only), status, extraction, response schema | **new** query/form/retries/extract/schema | 🟢 |
| SSRF protection (address checked when dialled; metadata/link-local always refused; private needs `allow_private`; no public→private redirects) | **new — fixed a real hole** | 🟢 |
| credentials never forwarded to another host on redirect | **fixed** (urllib forwarded `Authorization`) | 🟢 |
| SMTP, TLS, text, HTML, attachments (project files, no secrets), reply threading, recipient validation, approval, safe retries (only before hand-over) | **new** HTML/attachments/reply/retries | 🟡 (fake local SMTP; no real server) |

## Files and artifacts

| Feature | Now | Status |
|---|---|---|
| artifact id, metadata, MIME, size, checksum, retention, owner-only storage, events, CLI, web console | **new** | 🟢 |
| screenshots and downloads become artifacts | **new** | 🟢 |
| PDF (pdftotext), images, CSV, JSON, YAML, text, Office | present | 🟢 |

## MCP

| Feature | Now | Status |
|---|---|---|
| discovery, lifecycle, tools, schemas, calls, errors, timeouts, permissions, approval, audit | present | 🟢 |
| resources (list/read) | **new** | 🟢 (stdio fake server) |
| tool events (`tool.started/completed/failed`) for workflow calls | **new** | 🟢 |

## Agent architecture (Agent S, UI-TARS)

| Feature | Now | Status |
|---|---|---|
| observe → plan → ground → act → verify → reflect → recover → replan; planner/worker/verifier/reflector; bounded context; resume; cancellation | present | 🟢 |
| pause / resume between steps | **new** | 🟢 |
| "done" after exactly `max_steps` steps | **fixed** (was reported as failed) | 🟢 |
| separate planner and vision/grounding models | present | 🟡 (no model here) |
| multi-agent: manager + specialists, delegation, ownership, isolated contexts, budgets (steps/time/tokens), shared results as data, cancellation, verification, aggregation | **completed** | 🟢 |
| best-of-N with budgets, independent attempts, evaluator, cleanup | **new** (project copies; refused on shared screens) | 🟢 (scripted) · 🟡 with a model |

## Models

| Feature | Now | Status |
|---|---|---|
| OpenAI-compatible, Anthropic, Gemini (platform), local endpoints, vision, capability detection, timeouts, retries, token/cost tracking, consent, routing | present | 🟡 (scripted models in tests) |
| local model discovery (Ollama, OpenAI-compatible; loopback only) | **new** `highhx agent models --discover` | 🟢 (fake local servers) |
| model request/response/error events, planning latency | **new** | 🟢 |
| **a real local model through screenshot → model → grounding → policy → executor → verification** | not run | 🔴 **blocked**: no model server on this machine (installing Ollama was declined) |

## Memory, trajectories, skills

| Feature | Now | Status |
|---|---|---|
| typed project memory (task, strategy, failure, application, environment, preference), provenance, ranking, retention, deletion, injection protection | **completed** | 🟢 |
| trajectory search, outcomes, lessons as data | present | 🟢 |
| reproducibility metadata (versions, platform, planner, model, browser, project commit) | **new** | 🟢 |
| application skills (Chrome, GitHub, VS Code, Terminal, Gmail, files): versioned, validated against the catalog, examples runnable, notes to the planner as data | **new** | 🟢 (catalog/example level) |

## Human in the loop, sessions, events, observability

| Feature | Now | Status |
|---|---|---|
| approve, reject, **modify** (re-planned, asked again), **defer**, **timeout**, typed word for critical | **new** (queue-backed prompter) | 🟢 |
| task pause/resume/cancel/retry/replay/history; **fork**, **duplicate** | **completed** | 🟢 |
| one event bus with canonical names on every record; browser, computer.input, android.action, sandbox, model, grounding.failed, artifact events | **completed** | 🟢 |
| event timeline debugger: filter, search, export (JSON/JSONL/CSV) | **new** `highhx trace timeline` | 🟢 |
| web console: tasks, live state, plan, actions, network, history, approvals, live view, benchmarks, controls | **new** `highhx web` | 🟢 (real Chrome renders it; injection test) |
| TUI | present (+ network, intervention lines) | 🟢 |

## Benchmarks

| Feature | Now | Status |
|---|---|---|
| the eight official metrics | unchanged | 🟢 |
| diagnostics: per-action success, step/action/model/grounding/verification latency, attempts, failure categories, grounding confidence, recoveries, environment | **extended** | 🟢 |
| real-device Android benchmarks | present | 🟡 |
