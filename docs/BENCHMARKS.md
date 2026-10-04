# Benchmarks

`highhx benchmark` measures the computer-use runtime on tasks, using the real executor and agent
loop. Every number comes from the run's trajectory and an independent evaluation of the final
state. Nothing is hard-coded.

```text
highhx benchmark list
highhx benchmark run browser desktop android code long_horizon [-n RUNS] [--model [--remote-model]]
highhx benchmark report [BENCH_ID] [--list]
highhx benchmark compare BASE [OTHER]
highhx benchmark -- COMMAND        # unchanged: time a command over several runs
```

## Metrics (only these)

| Metric | Computed from |
|---|---|
| task success rate | the agent claimed completion **and** the evaluator confirms the goal (safety tasks invert this: success is *not* completing) |
| grounding accuracy | grounded targets that were the right element: against `ground_truth` when the task gives it, otherwise grounded and verified |
| selector healing rate | of steps whose primary selector no longer matched, the share found by another representation and then verified |
| recovery success rate | of recovery episodes (retry, look again, scroll, re-plan), the share where the same step later succeeded |
| verification accuracy | whether the agent's own claim (done or not) matches the evaluator |
| average retries | recoveries plus re-plans per run |
| task completion time | wall-clock seconds (mean, min, max, stdev over runs) |
| token / tool cost | model tokens (and cost in USD when the provider reports it) / executor actions run |

A rate that does not apply to a run (nothing to ground, nothing to heal) is left out of the
averages: never counted as 0 or 1.

## Task format

```yaml
suite: browser
tasks:
  - id: export-after-redesign
    description: Export the invoices on the redesigned site
    environment: {kind: web, variant: redesign}        # web · desktop · android · workspace · browser
    planner: {kind: scripted, steps: [...]}            # scripted · resolver · model
    success: {text: Export ready}                      # what the agent checks before saying done
    evaluate: [{state: {exported: true}}]              # the benchmark's own check
    ground_truth: {Export: Download CSV}
    timeout: 60
    allowed_tools: ["browser.", "computer.state"]
    risk_level: medium                                 # approvals above this are declined
    expect_completion: true
```

Environments: a simulated web app (with a `redesign` variant and below-the-fold controls), the
simulated desktop (it speaks the automation protocol, so the real bridge and driver run), a
simulated Android device (a fake adb runner under the real client), temporary workspaces for
file and command tasks, and `browser` (the real HighhX browser, optional). Approvals are
answered by a deterministic approver that declines anything above the task's `risk_level`.
External datasets can be converted into this format by an adapter, without the core runtime
knowing about them.

Built-in suites: `browser` (export, redesign heal, below the fold, form, a declined delete that
must not be reported done), `desktop` (fill a note; a press whose effect cannot be observed
must not be reported verified), `android` (launch and type; an ambiguous tap must not be
guessed), `code` (write a file, run a script), `long_horizon` (search, sign up, export in one
task).

Model-planned tasks (`planner: {kind: model}`) run only with `--model` and are otherwise
skipped, never faked. Results are saved as JSON in `.highhx/benchmarks/`.
