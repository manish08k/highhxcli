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

## Network evidence

Every tab enables the DevTools Network domain. A bounded journal
([`computer/network.py`](../src/highhx/computer/network.py)) keeps, per request: the method, a
**sanitized URL** (scheme, host, port, path and query parameter *names*; never their values,
never `user:password@`, never the fragment), the resource type, the status, or the failure
reason. Headers, cookies and bodies are never read. Each browser action takes the requests that
finished while it ran (plus a short wait for late responses) into its result
(`output.network`). That evidence is emitted as `network.observed`, stored with the trajectory
step, and used by the `network` verification check:

```yaml
- action: click
  target: {label: Place order, role: button}
  verify: {network: {url_contains: /api/orders, method: POST, status: 201}}
```

Each entry also has its duration (`ms`, from DevTools' own timestamps; absent rather than
invented when they are missing) and `redirect: true` on a hop answered with a redirect. Task
traces show a `Network` node per step and the live dashboard a `NETWORK` line.

Without a journal (for example, a remote browser that is not HighhX's), `network` stays
`unknown`, never guessed. Tested in real Chrome: `tests/unit/computer/test_live_network.py`.

### Waiting on the network

```yaml
- action: browser.wait
  parameters: {network_idle: true, idle_ms: 500, timeout: 30}           # nothing in flight for 500 ms
- action: browser.wait
  parameters: {request: {url_contains: /api/orders, method: POST, status: 201}, timeout: 10}
```

A `request` wait matches the sanitized URL (so never a query value), and also counts a request
that finished up to 2 seconds before the wait began (the click that sent it came first). Without
a journal the wait fails instead of guessing. Tested in real Chrome with a delayed, slow POST.

## Structured extraction

`browser.extract` with `schema` fills a JSON Schema (an object of string, number, integer,
boolean, array and object properties) from what the page states explicitly: `<label>` and its
control, `<dt>`/`<dd>`, a table row whose first cell is a header, `aria-label` on outputs,
`Label: value` lines, tables (arrays of objects, matched by their headers) and lists (arrays of
strings, matched by the heading before them). Properties match labels by their terms (snake_case
or camelCase names, or `title`, stemmed, with synonyms): every term of the property must be in
the label, so `total` matches "Order total" and not "Subtotal".

```yaml
- action: browser.extract
  parameters:
    schema:
      type: object
      properties:
        order_id: {type: string}
        total: {type: number}
        items: {type: array, items: {type: object, properties: {name: {type: string}, price: {type: number}}}}
      required: [order_id, total]
```

Output: `data`, `sources` (where each value came from), `missing` and `problems`. It never
guesses: two different values for one property are *ambiguous*, `"3 of 5"` is not a number, and
a missing required property fails the action. No model is used. Password, card, one-time-code
and hidden fields are never read. Tested in real Chrome (`tests/unit/computer/test_extract.py`).

## Recording and replay

```text
highhx browser record invoices --url https://app.example.com    # use the browser, Ctrl-C to stop
highhx browser show invoices
highhx browser replay invoices [--var PASSWORD=…] [--no-save-heals]
highhx browser heal invoices                                     # replay and save every heal
highhx browser workflows · delete NAME
highhx replay invoices                                           # the same; also replays task_… ids
```

`--url` is opened through the executor (`browser.open`: classified, policy-checked, audited)
before recording starts. The recorder itself never navigates or acts: it injects a listening
script through a DevTools binding (`Runtime.addBinding`, plus
`Page.addScriptToEvaluateOnNewDocument` so it survives navigation). It labels
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

## Fixes made for this work

The tab registry remembered the address HighhX last *requested* in a tab (to follow redirects),
but never forgot it when the page later moved elsewhere, for example after a person's click
while recording. "Open the start page" then believed the tab was already there and did
nothing. A request now stands for a tab only while the tab still shows the page that request
landed on (`Tab.landed`). Regression tests: `tests/unit/computer/test_tab_registry.py` and the
real-Chrome recorder test.

Turning on the Network domain exposed a transport bug. A read with a short deadline (pumping
events between actions) that expired while a large DevTools message was still arriving closed
the connection, because a half-read frame leaves the stream unusable. On busy real sites every
burst of Network events then cost a reconnect (6 in 15 navigations). A deadline now applies only
to *waiting for* a message. A message that has started arriving gets a bounded grace
(`FRAME_GRACE`, 10 s) to finish, and only a peer that stalls mid-message beyond it loses the
connection. Regression tests: `test_websocket_message_straddling_a_deadline_keeps_the_connection`,
`test_websocket_peer_stalling_mid_message_beyond_the_grace_closes`, and the real-internet test
(`test_github_then_wikipedia_repeatedly_on_the_real_internet`: 0 reconnects).
