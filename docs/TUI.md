# The HighhX console and live dashboard

```text
highhx tui
```

```text
╭─ HighhX  my-app · main · Pro · me@example.com · computer: local ─────────────────╮
│ TASK   export the invoices   running · 4.2s · verifying                           │
│ PLAN   ✓ open the invoices   ● export the invoices   [1/2]                         │
│ ACTION browser.click  target=button:"Export"   risk low · not needed              │
│ STATE  browser · https://shop.test/invoices · 3 element(s)                         │
│ GROUND 'Export'  accessibility ✗  dom ✓ 0.95                                       │
│ VERIFY success     recovery 0/10 · 1 healed                                        │
│ TOOLS  ✓ browser.open 0.3s  ✓ browser.click 0.3s                                   │
│ NETWORK POST https://shop.test/api/export 201                                      │
│ COST   0 tokens · 2 action(s) · avg 0.31s · runtime browser                        │
│ ⚠ healed 'Export' → 'Download CSV' (dom)                                          │
╰──────────────────────────────────────────────────────────────── trace tr_9c1… ──╯
❯ /trace
```

- Type a **goal** to run it (the agent loop, with Free's resolver by default, or `/run GOAL
  --model`, `--plan FILE`).
- **Slash commands**: `/run`, `/resume`, `/replay`, `/record`, `/workflows`, `/trace [ID]`,
  `/traces`, `/history`, `/state`, `/ground`, `/drivers`, `/android …`, `/sandbox …`,
  `/benchmark`, `/cli ARGS` (any HighhX command), `/palette [WORDS]`, `/help`, `/clear`, `/quit`.
- **Keyboard**: Tab completes commands (type `/` then Tab for the palette); ↑/↓ and Ctrl-R
  search history (kept between sessions); Ctrl-C cancels the running task, which stays
  resumable with `/resume`; Ctrl-D leaves.
- **Approvals** are asked in the console. The live view pauses first so the full request (risk,
  reasons, target) is visible.
- **NETWORK** shows the latest requests browser actions caused (sanitized URLs, status, and how
  many failed); **YOU** appears when the task is waiting for you (a CAPTCHA, a secret field, an
  unconfirmable result) with the reason.
- Output stays in the terminal's scrollback. `/history` lists past tasks and `/trace` opens the
  last task's trace.

## Architecture

The console adds no way to act. Every slash command is a HighhX CLI command run in-process with
a fresh application context (`/trace show X` is `highhx trace show X`), and plain text is
`highhx agent loop TEXT`. The dashboard (`ui/live.py`) is a pure view: `DashboardState.apply`
folds events from the stream ([EVENTS.md](EVENTS.md)) into what is shown, and `render` draws it.
The same dashboard is shown live by `highhx agent loop`, `highhx android agent`, `highhx browser
replay` and `highhx replay` (`--no-live` turns it off; it is off with `--json` or without a
terminal).

The header facts (project, branch, Free/Pro, account, computer target) come from the
repository and the cached account. Starting the console makes no network call.
