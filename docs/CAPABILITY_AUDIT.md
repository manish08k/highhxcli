# Computer-use capability audit

What HighhX has, established from the code and from real runs — not from other documents. Each
row names its evidence; a status is only as strong as the evidence beside it.

Statuses: **COMPLETE** (production code on the executor path, real-environment tests on this
build), **PARTIAL** (works, with a named gap), **OPTIONAL DEPENDENCY** (complete code that needs a
tool not installed here), **PLATFORM LIMITED** (one platform validated; others implemented but only
tested with fakes), **NOT IMPLEMENTED**.

## Baseline (2026-10-06, before this phase's changes)

| | |
|---|---|
| Tests | 2517 passed, 61 skipped (opt-in real-platform tests), 0 failed — `python -m pytest tests` |
| Real Chrome | 46 passed — `HIGHHX_TEST_BROWSER=1` (live browser, network, sessions, profiles, livestream, extract, recorder, web console, live benchmark) |
| Real desktop (macOS 27, AppKit fixture) | 19 passed — `HIGHHX_TEST_DESKTOP_INPUT=1 tests/e2e/test_desktop_live.py` |
| Real terminal | `highhx tui` in a pseudo-terminal — `tests/unit/ui/test_tui_console.py` |
| Lint / types | ruff 15 findings, mypy 8 errors (pre-existing, unchanged files) |
| Not on this machine | adb, Android emulator, tesseract (OCR), Lima, Tart, Docker, bubblewrap, xdotool, Ollama; no remote browser/computer configured |

No `TODO`/`FIXME` in `src/`; every `NotImplementedError` is an abstract base method.

## Matrix

| Capability | Implementation (evidence) | Gaps found | Status |
|---|---|---|---|
| Unified execution | `ActionExecutor` (plan → classify → policy → approval → run → verify → audit) for every `computer.*`, `browser.*`, `android.*`, `vm.*` action; agent loop, workflows, web console, TUI and MCP all call it | — | COMPLETE |
| Screenshot | `computer.screenshot` (screen, window, region; `CaptureStore` grounding, staleness), `browser.screenshot` (+ page capture), `android.screenshot`; artifacts with retention | No redaction of secret fields in stored images | PARTIAL → fixed below |
| Mouse | move, click, double/right/middle, down/up, drag, scroll incl. horizontal, hover — real AppKit fixture tests, edge hits on a 10pt control, clicks after window move/resize | — | COMPLETE (macOS) |
| Keyboard | type (Unicode, Caps-Lock-proof), keys, combos, forward delete, edit shortcuts; browser: all keys + macOS editing commands — real desktop and real Chrome tests | — | COMPLETE |
| Clipboard | `computer.clipboard_read/write`, `computer.edit` (copy/cut/paste/undo); paste asked first; real tests | — | COMPLETE |
| Windows/apps | launch, focus (app or exact window), quit, window list, frame, maximize/minimize (macOS menu) — real tests | Minimize on Windows/Linux refused ("not on this platform yet") | PLATFORM LIMITED |
| Observation | `computer.state` fuses DOM / accessibility / screenshot / OCR / vision with timestamps, secret values blanked in structure | — | COMPLETE |
| Vision / grounding | `grounding/` providers (accessibility, DOM, OCR, local or remote vision model), confidence, fallback order, `computer.ground` | OCR and vision models not installed here | OPTIONAL DEPENDENCY |
| Browser hybrid | DOM actions first; `browser.click_at` / `browser.scroll` from a page capture (stale capture refused; pinch-zoom-correct) — real Chrome tests | — | COMPLETE |
| Observe → act → verify | agent loop worker/verifier/reflector, `NO_PROGRESS`, max steps/failures/time; runtime verification per action | — | COMPLETE |
| Recovery | reflector + recovery strategies (re-ground, scroll, wait, replan); browser reconnect never repeats an unsafe action — real Chrome crash/kill tests | — | COMPLETE |
| Trajectory / audit | `TrajectoryStore`, `TraceStore`, audit log, events — one history | — | COMPLETE |
| Checkpoint / resume | `agent loop --resume`, workflow resume; verification before continuing | — | COMPLETE |
| Pause | agent loop pause between steps; web console pause/resume/cancel | — | COMPLETE |
| Human takeover | — | No takeover/release: nothing stops the agent acting while a person uses the computer, and nothing forces a fresh observation afterwards | NOT IMPLEMENTED → implemented below |
| Dry run | global `--dry-run` reaches every action (nothing changes) | **Bug:** `--dry-run agent loop` reported "✓ completed: all 2 step(s) done", without the plan, risks or approvals | PARTIAL → fixed below |
| Rollback | `compensate` on specs with a real inverse (`/undo` of file changes) | — | COMPLETE |
| Browser sessions | profiles, managed sessions, heartbeat, reconnect, downloads, uploads, popups, crash recovery, no duplicate tabs — real Chrome | — | COMPLETE |
| Remote browser | `RemoteBrowser` (wss/http DevTools, token kept out of storage) | Not configured here (no endpoint to test against) | REQUIRES EXTERNAL SERVICE |
| Remote computer | `ssh://` runtime, keys only, host keys checked, `remote.check` | No host configured here | REQUIRES EXTERNAL SERVICE |
| VM | `vm.*` on Lima/Tart command lines | Neither installed; validated with a fake runner only | OPTIONAL DEPENDENCY |
| Android | `android.*` over adb (25 actions), emulator lifecycle, hierarchy, vision fallback | No adb/emulator here; tested with a fake adb | OPTIONAL DEPENDENCY |
| macOS | AX + Quartz backend — real tests | — | COMPLETE |
| Windows / Linux | UI Automation / X11 + AT-SPI backends | Only fake-runner tests on this machine; Wayland refused | PLATFORM LIMITED |
| Native file dialogs | browser uploads avoid them (`browser.upload`) | Desktop open/save panels: no structured support | NOT IMPLEMENTED (see below) |
| Memory | long-term `agent/memory.py` (typed, provenance, secrets refused); in-task attempts in the reflector | — | COMPLETE |
| Skills | `highhx skills`, application skills on the executor | — | COMPLETE |
| Scheduling / triggers | `automation/scheduler.py` (cron schedules, file watches) | — | COMPLETE |
| Security | risk classes, approvals, policy rules, path confinement, secret redaction, SSRF, CSP/CSRF console | **Gap:** domain and application policies only see an action's own target: a rule against `bank.com` stops `browser.open` but not a click or typing on that site. **Bug found while fixing it:** the gate dropped `require_approval` decisions, so such rules never asked for any executor action | PARTIAL → fixed below |
| Benchmarks | `highhx benchmark` suites, 8 metrics, real-browser benchmark | Android/OSWorld need their environments | PARTIAL |
| Live view | web console (screencast, status, approvals), TUI live dashboard | — | COMPLETE |

## Gaps, in the order they are addressed

1. **Dry run reports success** — a planning run must show the plan, each step's risk and whether
   it needs approval, and end as a plan, never as "completed". *Done:* status `planned`.
2. **Domain and application policies** for every browser and desktop action, enforced by the
   existing policy engine (the page's host for browser actions; the application for desktop) —
   and `require_approval` rules honoured by the gate. *Done:* `host` / `app` rule conditions,
   `tests/unit/test_site_app_policies.py`, real Chrome `known_url` test.
3. **Human takeover** — pause the agent, refuse its actions while a person operates the
   computer, and require a fresh observation on release.
4. **Screenshot redaction** — blank the bounding boxes of secret fields (passwords, card
   numbers, one-time codes) in stored screenshots, from DOM/accessibility, and say so in
   metadata (no claim of general PII detection).
5. **Native file dialogs** — macOS open/save panels through accessibility (Go-to-folder path
   entry, then verify); other platforms reported as unsupported.
