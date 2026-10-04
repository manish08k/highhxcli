# Task traces

A task trace lets you reconstruct a run afterwards: what was asked and planned, what was
observed, which actions ran (with risk, approval and the history entry), what failed or was
retried, recovered or healed, what was verified, and how it ended. It is built only from the
task's events.

```text
Task          'Export the invoices'  [completed]  — tr_0c04e059d287 · task_a04a2e61455b
├── Plan          2 step(s)  — open the invoices · export the invoices
├── Step          1  open the invoices  [done]
│   ├── Action        browser.open  [ok]  — risk low · not asked · 0.33s (execution 20261003T…)
│   ├── Observation   browser · 2 element(s)  — https://shop.test/invoices
│   ├── Verification  success  — 1 observation(s)
│   └── Reflection    continue  — verified
├── Step          2  export the invoices  [done]
│   ├── Grounding     'Export'  [grounded]  — accessibility failed → dom success
│   ├── Action        browser.click  [ok]  — risk low · not asked · 0.31s (execution …)
│   ├── Network       1 request(s)  — POST https://shop.test/api/export → 201 84ms
│   ├── Verification  success
│   └── Healed        'Export' → 'Download CSV'  — dom, accessibility no longer matched
├── Verification  satisfied
└── Result        completed  — all 2 step(s) done
```

```text
highhx trace list
highhx trace show tr_… | task_…        # also: highhx trace tr_…
highhx trace export tr_… [--format json|jsonl] [-o FILE]
highhx trace [EXECUTION_ID]            # unchanged: an execution's timing tree (spans)
```

Traces are redacted JSON Lines, one per trace id, in `.highhx/state/traces/` (or the user data
directory). They are written by `highhx agent loop`, browser replays, `highhx replay`, the TUI,
and any `AgentLoop` given a `TraceStore`, including on interruption. The execution ids in a trace
link to `highhx history` and `highhx trace EXECUTION_ID`, and the task id links to `highhx
trajectories show`.

Network evidence appears as a `Network` node in the step that caused it (count, failures, the
first requests with status and duration; URLs without query values).
