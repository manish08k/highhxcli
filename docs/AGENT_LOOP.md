# The computer-use agent loop

```text
TASK ── route the surface ── outline ── lessons from similar past tasks
  loop
    OBSERVE    computer.state (an audited action), structure first
    PLAN       the planner proposes ONE step (data)
    WORK       ground the target, submit ActionRequest(s) → ActionExecutor
    VERIFY     a declarative check on a fresh observation. UNKNOWN → observe again
    REFLECT    continue · retry · look again · scroll · re-plan · stop · ask the person
    RECORD     trajectory step + checkpoint + events
  DONE         the task's success check on a fresh observation (never assumed)
```

Code: [`src/highhx/agent/loop/`](../src/highhx/agent/loop). The loop has no way to act of its
own. Every action is an `ActionRequest` run by the one `ActionExecutor`: risk classification,
policy, approval, the action's own verification, audit, history.

## Roles

| Role | Class | What it does |
|---|---|---|
| Planner | `ScriptedPlanner` | A fixed list of semantic steps (plans, recorded workflows, replays, benchmarks). Free |
| | `ResolverPlanner` | HighhX Free's deterministic language resolver (`highhx do …`), as steps. No AI |
| | `ModelPlanner` | A `LanguageModel` proposes one step from the task, the screen (marked untrusted), history with outcomes and reflections, and lessons from memory. Its JSON is checked against a closed set of verbs and the task's allowed actions. HighhX Pro or a local model |
| Worker | `AgentWorker` | Maps a step to catalog actions for the surface (see below), grounding targets first |
| Observer | `AgentObserver` | Runs `computer.state`, and fetches OCR or vision only for grounding when the task allows it |
| Verifier | `AgentVerifier` | The step's `verify` check, or a default per verb. `UNKNOWN` triggers more observations before it stands |
| Reflector | `AgentReflector` + `RecoveryManager` | Decides what happens next and why, within bounds |

Step verbs: `click`, `double_click`, `right_click`, `type`, `press`, `hotkey`, `scroll`, `open`,
`launch`, `back`, `home`, `select`, `wait`, or any catalog action name (`shell.run`, `filesystem.write`,
`api.request`, `android.launch` …) that the task allows.

| Verb on … | browser | desktop | android |
|---|---|---|---|
| click (found in DOM/AX) | `browser.click 'role:"name"#k'` | `computer.click_at text=…` (re-grounded and window-checked at click time) | `android.tap x,y` |
| click (found by OCR/vision) | `browser.click_at x,y` | `computer.click_at x,y` | `android.tap x,y` |
| type | `browser.fill` (or click + `browser.insert_text`) | click + `computer.type` | tap + `android.type` |
| right_click | `browser.click_at x,y button=right` | `computer.click_at x,y button=right` | `android.long_press x,y` |
| hotkey | — (the browser presses one key at a time) | `computer.hotkey keys` | — |

Every request carries the target's **label**, so the executor rates `Delete account` as a
destructive control, whatever coordinates grounding produced.

## Outcomes

`SUCCESS`, `PARTIAL_SUCCESS`, `FAILED`, `UNKNOWN`. A UI action whose handler returned without a
check is `UNKNOWN`, never success. Default checks:

| Verb | Check |
|---|---|
| click, press, select, back, home, scroll | the screen changed |
| type | the field shows the text (or the text appears) |
| open | the browser is on that host |
| launch (desktop) | that application is in front |

Text typed into a secret field (password, card, one-time code) cannot be read back. The step
is recorded as unconfirmed (`unobservable`), the loop may continue, and the task's own
success check decides. Without a success check, such a task ends as `needs_user`, not
`completed`.

## Recovery (bounded)

| Situation | What happens |
|---|---|
| Declined by the person, blocked by policy, cancelled | stop: final |
| Target not found | grounding has already escalated through every allowed strategy; then look again, then scroll, then re-plan |
| Several equal matches | re-plan (be more specific), never pick one |
| Failed, nothing ran / SAFE / idempotent | retry (a new request: classified and approved again) |
| Failed or timed out, may have run, riskier than SAFE | never repeated silently: re-plan from a fresh observation |
| Verified failure, LOW risk | retry once, then re-plan |
| UNKNOWN after extra observations | re-plan |

**Human-verification challenges.** Before each planning step the screen is checked for a
CAPTCHA (provider endpoints such as `google.com/recaptcha`, `hcaptcha.com`,
`challenges.cloudflare.com`, and the providers' own wording such as "I'm not a robot"). When one
is there the task stops as `needs_user` with the evidence and the resume command; the person
solves it. Ordinary pages that mention robots do not trigger it.

Limits: steps, failures, re-plans, recoveries per step (3) and in total, wall-clock time, and a
no-progress detector (the same screen three times while steps keep failing). Every recovery
emits `recovery.started` and `recovery.completed`.

## Checkpoints and resume

After every step the trajectory is saved with a checkpoint: the task, the planner's state (a
script's cursor), the counters and the surface. Interrupted (Ctrl-C), failed or `needs_user`
tasks resume with `highhx agent --resume task_…` (or `highhx agent loop --resume`). Resume keeps
the same task id and trace id and continues at the next step. A model planner continues from
the history so far. Secret text is never stored in a checkpoint: a resumed step that needs it
stops and asks again.

## Tool routing

`ToolRouter` picks the surface when the task does not name one. It is deterministic and uses
cues in the request, cost (files and commands are cheaper than screens, and screens read by a
vision model cost the most), risk, availability (no adb, no browser) and past success from
trajectory memory. It only chooses a surface: every action still goes through the executor.

## Specialists (optional)

`SupervisorAgent` splits a task (with a model, or by the router's distinct tools) and runs
specialists in order on the same executor, trace store and trajectory store: `research`,
`browser`, `computer`, `android`, `code`, `testing`, `debugging`. A specialist is an
`AgentLoop` with a narrower `allowed` list and a surface. It has no other powers. Simple tasks
never use the supervisor; one agent is the default.

## CLI

```text
highhx agent loop "export the invoices" --surface browser --plan steps.yaml --success '{"text": "Export ready"}'
highhx agent loop "…" --model [--remote-model] [--vision]
highhx agent --resume task_…
highhx android agent "…" --plan steps.yaml
highhx tui
```
