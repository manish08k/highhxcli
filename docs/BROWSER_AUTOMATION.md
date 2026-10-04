# Browser automation

The HighhX browser is a Chromium-family browser on its own profile, driven over the Chrome
DevTools Protocol ([ARCHITECTURE.md](ARCHITECTURE.md#the-highhx-browser)). Its transport, tab
registry, single recovery policy (safe actions retried, unsafe ones never repeated once they
may have run), dialogs, popups and downloads already existed. This page covers the computer-use
layer on top of it.

## Actions

`browser.open`, `click`, `fill`, `select`, `press`, `scroll`, `hover`, `double_click`, `drag`,
`upload`, `download`, `back`, `forward`, `refresh`, `new_tab`, `close_tab`, `switch_tab`,
`tabs`, `wait`, `find`, `extract`, `screenshot`, and for targets found by OCR or vision,
`browser.click_at` (CSS pixels) and `browser.insert_text`. All are catalog actions run by the
executor. `browser.open` reuses a tab that already shows the address ("open Gmail" twice gives
one Gmail tab).

## State and grounding

`computer.state --surface browser` gives the DOM structure with roles, accessible names, stable
attributes (`dom_id`, `testid`, `name`, `href`, classes, `placeholder`) and bounds, plus the URL,
title, tabs and loading state. A screenshot is taken only when asked for (or when OCR or vision
must run) and is in CSS pixels, so a box found in it can be clicked as it is. See
[PERCEPTION.md](PERCEPTION.md) and [GROUNDING.md](GROUNDING.md).

## Recording and replay

```text
highhx browser record invoices --url https://app.example.com    # use the browser, Ctrl-C to stop
highhx browser show invoices
highhx browser replay invoices [--var PASSWORD=…] [--no-save-heals]
highhx browser heal invoices                                     # replay and save every heal
highhx browser workflows · delete NAME
highhx replay invoices                                           # the same; also replays task_… ids
```

The recorder injects a script through a DevTools binding (`Runtime.addBinding`, plus
`Page.addScriptToEvaluateOnNewDocument` so it survives navigation). It never acts. It labels
each clicked, typed, selected or Enter-pressed element with **the same role and name rules as
observation**, and keeps its attributes, box and viewport. Events become semantic steps:

- a click into a field followed by typing is one `type` step;
- a navigation right after a click becomes that click's check (`url_contains`);
- any other navigation is an `open` step;
- typing gets an element-value check;
- **text typed into password, card or one-time-code fields is never recorded**: it becomes a
  variable (`--var NAME=…` or `HIGHHX_VAR_NAME` at replay).

Workflows are JSON in `.highhx/browser-workflows/` (or the user data directory outside a
project), redacted when written. Replay runs through the agent loop, so every step is
grounded, approved, verified and recovered, and drifted selectors are healed
([SELF_HEALING.md](SELF_HEALING.md)).

## A fix made for this work

The tab registry remembered the address HighhX last *requested* in a tab (to follow redirects),
but never forgot it when the page later moved elsewhere, for example after a person's click
while recording. "Open the start page" then believed the tab was already there and did
nothing. A request now stands for a tab only while the tab still shows the page that request
landed on (`Tab.landed`). Regression tests: `tests/unit/computer/test_tab_registry.py` and the
real-Chrome recorder test.
