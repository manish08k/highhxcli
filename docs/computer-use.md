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

## Goal tasks (Task IR)

`highhx computer task` works toward a goal instead of running a fixed list: it observes the
page, chooses the next generic action, executes it through the runtime above (safety policy,
confirmation, audit), verifies the expected result, and recovers or replans when a step
fails. Every run is logged as `TASK`, `PLAN`, `OBSERVE`, `ACTION`, `RESULT`, `VERIFY`,
`RECOVERY` and `FINAL`, and written to `<data dir>/tasks/<task id>.jsonl`.

```bash
highhx computer task "open YouTube and play adhento gani"    # Free: from the deterministic resolver
highhx computer task --ir contact.json                       # any site, from Task IR you write
highhx --dry-run computer task "open github"                 # show the Task IR, run nothing
highhx computer task --schema                                # the JSON Schemas
```

A task is structured JSON, validated before anything runs:

```json
{
  "goal": "Find the shop's contact email",
  "context": {"start_url": "https://books.example"},
  "constraints": {"max_steps": 20, "timeout": 120, "stay_on_site": true},
  "steps": [
    {"action": "browser.navigate", "value": "https://books.example", "reason": "open the site"},
    {"if": {"element": "button:Accept cookies"},
     "then": [{"action": "browser.click", "target": "button:Accept cookies", "reason": "dismiss the banner"}]},
    {"action": "browser.click", "target": "link:Contact", "reason": "contact details live there",
     "expect": {"url_contains": "/contact"}},
    {"action": "browser.read", "value": "[\\w.+-]+@[\\w-]+\\.[\\w.]+", "reason": "extract the address"}
  ],
  "success_conditions": [{"text_matches": "@"}],
  "failure_conditions": [{"text": "Access denied"}],
  "allow_replanning": true
}
```

- **Primitives** (generic, never per site): `browser.navigate`, `observe`, `find`, `click`,
  `type`, `read`, `scroll`, `select`, `press`, `wait`, `new_tab`, `close_tab`, `switch_tab`,
  `back`, `forward`, `upload`, `download`, `screenshot`, `verify`, `recover` — plus the planner
  decisions `task.done`, `task.fail`, `task.ask_user`. There is no primitive that runs code or
  commands, and the schemas are closed: an unknown field or action is an error.
- **Targets** are discovered from the page: an element id from the last observation (`e12`), a
  role and name (`button:Search`, `textbox="Email"`, `link#2`), attribute filters
  (`link[href*=/watch]#1`).
- **Conditions** (`expect`, success and failure conditions, `if`, `repeat … until`):
  `url_contains`, `title_contains`, `text`, `text_matches`, `element`, `absent`,
  `media_playing`, `download_completed`.
- **Control flow**: `{"if": condition, "then": [...], "else": [...]}` and
  `{"repeat": {"steps": [...], "until": condition, "max": n}}` (at most 25 iterations, nested at
  most 3 deep).

**Who plans.** On HighhX Free the steps come from you (`--ir`) or from the deterministic
resolver — the target registry supplies a site's address, search URL and what a result link
looks like; there is no per-site code, and a site that is not in the registry is just a URL.
On HighhX Pro, a request nobody programmed ("open LeetCode and solve one problem") is turned
into Task IR by the AI planner, which then proposes one validated action at a time from the
page's accessibility tree (page text is passed as untrusted data). Pro also takes over when a
deterministic plan fails (`allow_replanning`). The planner's actions run as the **agent**: it
cannot type passwords or payment details, and sending, submitting, buying or deleting asks you.

**Recovery, never blind repetition.** After a failure HighhX observes the page again, then:
waits for a slow element; scrolls to reveal a missing one and retries once (nothing happened
the first time); retries only actions that cannot happen twice (navigate, read, scroll …) after
a lost connection; never repeats a click, keystroke or submission whose outcome is unknown; and
otherwise hands the problem to the planner. An action that failed on a page is never proposed
again on that same page. Limits end every task: `max_steps`, `max_failures`, `timeout`,
per-action timeouts, a repeat cap per action and a no-progress detector. Ctrl+C cancels.

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

## Native desktop control (the HighhX Computer Runtime)

Beyond the browser, HighhX observes and operates desktop applications on macOS, Windows and
Linux: the accessibility tree with element positions, windows and applications, screenshots,
clicks at points (double, right, in the background), drags, wheel scrolling, menus, window
geometry and the clipboard — `highhx computer windows`, `click --at X,Y`, `menu APP "File > Save"`
… and, for HighhX Pro's agent, the same operations through `computer_act`. Every one is
classified, approved and audited; input never reaches a terminal. What each platform supports,
what has been tested where, and how it relates to Cua: [COMPUTER_RUNTIME.md](COMPUTER_RUNTIME.md).

