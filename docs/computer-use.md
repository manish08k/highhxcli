# Computer use

HighhX operates browsers and desktop applications **semantically** — controls by role and
name (`button "Save"`, `textbox "Email"`, `checkbox "Remember me"`), not screen coordinates.

| | Free | Pro |
|---|---|---|
| Who picks the action | You: a command, a selector or a flow file | The AI agent, from the valid actions for the current UI |
| Commands | `highhx computer …`, `highhx do …` | `highhx agent` (`computer_observe`, `computer_act`, `browser_open`, `app_open`) |
| Safety, confirmation, verification, audit | same | same |

## Perception (accessibility-first)

| Provider | Status | Notes |
|---|---|---|
| Native accessibility — macOS | implemented, not yet verified end-to-end | System Events (JXA). Grant your terminal Accessibility permission. |
| Native accessibility — Windows / Linux | **not implemented** | Reported by `highhx computer status`; use browser automation. |
| Browser DOM — Chrome, Chromium, Edge, Brave | implemented (see verification status below) | DevTools protocol on 127.0.0.1, dedicated HighhX profile (never your personal one). |
| Local OCR | implemented, not yet verified end-to-end | Needs the `tesseract` binary and a screenshot tool; output parsing is tested. |
| Vision model | **not bundled** | Optional `VisionProvider` interface for plugins; never required. |

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
highhx computer press enter
highhx computer click "button:Sign in"            # submits a form → asks to confirm
highhx computer type textbox:Password --from-env SITE_PASSWORD   # never shown or logged
highhx computer run login-flow.yaml
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

## Verification status in this release

The runtime, safety rules, flows and the DevTools client are covered by tests (the page
scripts run against a real DOM implementation, the transport against a DevTools-protocol
server). Driving a real Chrome instance is covered by an opt-in test
(`HIGHHX_TEST_BROWSER=1`). macOS Accessibility requires the permission above.
