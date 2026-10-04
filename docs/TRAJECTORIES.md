# Trajectories

Every agent-loop task is recorded as a trajectory: what it saw, did, got, checked and
concluded, step by step.

| Per step | Content |
|---|---|
| `observation` | fingerprint, surface, URL or app, title, element count, screenshot reference (never pixels) |
| `action` | the `ActionRequest` (typed text replaced by its length) and the semantic step (verb, target, parameters) |
| `result` | the `ActionResponse`: outcome, executor status, risk, approval, error, execution id |
| `verification` | the declarative report, or `unobservable` for secret fields |
| `reflection` | the decision (continue, retry, look again, scroll, re-plan, stop) and the reason |
| `grounding` | every strategy attempted, the candidate, and **all representations of the element found** |

Plus the plan, metrics (actions, observations, failures, re-plans, recoveries, tokens, cost,
grounding strategies, outcomes, seconds) and the checkpoint used by resume
([AGENT_LOOP.md](AGENT_LOOP.md#checkpoints-and-resume)).

Storage: redacted JSON, one file per task, in `.highhx/trajectories/` (or the user data
directory). Text typed into secret fields is registered with the redactor *before* the action
runs and is never written.

## Memory

- `search(query)`: similar past tasks. Lexical by default (`HashingEmbedding`, no model);
  any `EmbeddingModel` can replace it.
- `hints(label, url, app)`: selectors that found a target before. The worker uses them first.
- `lessons(query)`: short notes for the model planner (what worked, what failed and why).
- Router history: success rate per surface ([AGENT_LOOP.md](AGENT_LOOP.md#tool-routing)).

## Replay

`replay_steps(trajectory)` returns the semantic steps. Coordinates are dropped whenever a
target is known, and the replay grounds every target again on the current screen. Steps
whose text went into a secret field cannot be replayed without being given the text again.

```text
highhx trajectories list [--status failed] · show task_… · search "export invoices"
highhx replay task_…
```
