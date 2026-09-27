# Computer use

HighhX operates browsers and desktop applications **semantically** — controls by role and
name (`button "Save"`, `textbox "Email"`, `checkbox "Remember me"`), not screen coordinates.

| | Free | Pro |
|---|---|---|
| Who picks the action | You: a command, a selector or a flow file | The AI agent, from the valid actions for the current UI |
| Commands | `highhx computer …`, `highhx do …` | `highhx agent` (`computer_observe`, `computer_act`, `browser_open`, `app_open`) |
| Safety, confirmation, verification, audit | same | same |

## Perception (accessibility-first)

| Provider | macOS | Windows | Linux | Notes |
|---|---|---|---|---|
| Browser DOM (`--source browser`) — Chrome, Chromium, Edge, Brave | ✓ | ✓ | ✓ | DevTools protocol on 127.0.0.1, dedicated HighhX profile (never your personal one). Observe, click, type, select, press, scroll, navigate. |
| Native accessibility (`--source desktop`) | ✓ | not implemented | not implemented | macOS System Events (JXA); grant your terminal Accessibility permission. Elsewhere the source is refused with a clear message. |
| Screen text via local OCR (`--source screen`) | ✓ | not implemented | ✓ (X11) | **Read-only**: observe only, nothing on it can be clicked or typed. Needs `tesseract` plus `screencapture` (macOS, Screen Recording permission) or ImageMagick `import` (X11). |
| Vision model | — | — | — | **Not bundled.** `VisionProvider` is an interface for plugins; nothing requires it. |
| Launch applications (`highhx computer open <app>`) | ✓ (verified running) | ✓ (not verified) | ✓ (not verified) | Uses the platform launcher. |

The providers implement the same interfaces (`ComputerUseProvider`, `BrowserProvider`,
`AccessibilityProvider`, `OCRProvider`, `VisionProvider` in `highhx.computer.providers`);
the runtime, safety and verification layers are platform-independent.

`highhx computer status` shows what is available on your machine.

## The loop

```text
observe → semantic UI (roles, names, states) → finite action candidates
        → choose (you, a flow, or the AI) → safety classification + confirmation
        → re-observe and re-bind the control → execute → observe again → verify
        → done / retry discovery / ask / replan
```

Action candidates are ids such as `click:e12`, `type:e4`, `select:e7`, `press:enter`,
`scroll:down`, `done`, `ask_user`. The AI can only choose one of these — it cannot send
coordinates, scripts or arbitrary key codes.

Verification examples: a typed value is read back (never for secret fields); a checkbox
must toggle; a click must change the page; a navigation must reach the requested page.
An action with no observable effect is reported as **not verified**.

Password and payment fields never have their values read, and the agent may not type into
them. Page and app text is passed to the model as untrusted data.

## Deterministic automation (Free)

```bash
highhx computer status
highhx computer open https://example.com          # or: highhx computer open Calculator
highhx computer observe --actions                 # controls + valid action ids
highhx computer type "searchbox:Search" "Adele"
highhx computer press enter                       # submits the form → asks to confirm
highhx computer click "button:Sign in"            # submits a form → asks to confirm
highhx computer observe --source screen           # screen text via local OCR (read-only)
highhx computer type textbox:Password --from-env SITE_PASSWORD   # never shown or logged
highhx computer run login-flow.yaml               # in CI: highhx --yes computer run login-flow.yaml
highhx computer browser stop
```

Selectors: `Search` (name contains), `button:Search` (role + name), `textbox="Email"`
(exact), `link:Docs#2` (second match).

```yaml
# login-flow.yaml
name: search
timeout: 10              # seconds to wait for targets to appear
steps:
  - open: https://shop.example.com
  - type: {into: "searchbox:Search", text: "Adele"}
  - press: enter
  - expect: {text: "results for Adele", url_contains: "/search"}
  - launch: Calculator
```

`highhx do` maps fixed, unambiguous requests to commands without AI: `highhx do run the
tests`, `highhx do open chrome and search for Adele`, `highhx do open localhost:3000`.
Requests that need understanding are refused with a pointer to `highhx agent`.

## Reliability

- A browser command is sent **once**. If the connection drops or the browser crashes after a
  command was sent, the result is reported and audited as **unknown** (it may have happened)
  and that action is never repeated automatically — not after a reconnect either; the next
  decision starts from a fresh observation.
- After an action, HighhX waits for any navigation it started (form submit, link) to finish
  before observing again, so verification sees the new page.
- Every step is cancellable (Ctrl+C, `highhx agent stop`): DevTools calls, accessibility
  scripts and OCR subprocesses (and their children) are stopped.

## Verification status in this release

| What | How it is verified |
|---|---|
| Runtime, safety rules, flows, confirmation binding | Unit tests with a scripted UI |
| Page scripts (DOM observation, actions) | Against a real DOM implementation (jsdom, `HIGHHX_TEST_JSDOM`) |
| DevTools transport, lost answers, crashes, timeouts | Against a local DevTools-protocol server |
| **Real Chrome** (search flow, runtime + gate + verification, reconnect without repeats) | `HIGHHX_TEST_BROWSER=1 pytest tests/unit/computer/test_live_browser.py` — passed on macOS with Google Chrome |
| macOS Accessibility end-to-end | Not yet verified (needs the permission on an interactive machine) |
| Real Tesseract OCR | `HIGHHX_TEST_OCR=1 pytest tests/unit/computer/test_live_ocr.py` (needs tesseract) — not yet verified; the pipeline is tested with stand-in executables |

Sensitive actions (including pressing Enter in a form, which submits it) ask for
confirmation. In scripts and CI, `--yes` confirms **your own** deterministic actions; the
agent's sensitive actions always need a person.
