# Self-healing execution

When a step's target cannot be found the way it was recorded, HighhX looks for it by its other
representations, re-grounds, retries through the executor, verifies, and records the healed
strategy.

```text
recorded selector (accessibility: button "Export")      ✗ no longer matches
  → DOM (data-testid=export-invoices)                   ✓ found "Download CSV"
  → browser.click 'button:"Download CSV"'               (classified · approved · audited)
  → verify (the screen changed / the expected text)     ✓
  → selector.healed event · trajectory · saved to the workflow
```

## Order

Semantic and structural recovery comes before pixels and coordinates:
accessibility → DOM attributes (test id, id, href, name, stable classes) → visible text → OCR →
vision (if allowed) → coordinates (only in the same page and viewport, and only when the task
trusts them). See [GROUNDING.md](GROUNDING.md).

If every representation fails, the loop's recovery takes over (look again, scroll, re-plan;
see [AGENT_LOOP.md](AGENT_LOOP.md#recovery-bounded)). Every retry is a new `ActionRequest`, so
risk is evaluated again and approval asked again when needed. A risky action that may have run
is never repeated silently.

## What a heal records

| Field | Example |
|---|---|
| `old_selector` / `new_selector` | `{"label": "Export", "accessibility": {...}}` → `{"label": "Download CSV", ...}` |
| `reason` | `accessibility no longer matched; found by dom` |
| `strategy`, `confidence` | `dom`, `0.95` |
| `when`, `step` | timestamp, step number |

Heals are emitted as `selector.healed`, appear in the task trace (`Healed 'Export' → 'Download
CSV'`) and the live dashboard. A replayed browser workflow saves them back (`--save-heals`,
the default), so the next replay matches directly.

## Tested

- Simulated redesign (labels, classes and layout change; the test id stays), in the agent loop,
  replay and the browser benchmark suite (`export-after-redesign`).
- Real Chrome (opt-in, `HIGHHX_TEST_BROWSER=1`): `tests/unit/computer/test_live_recorder.py`
  records a person's clicks, redesigns the site and replays with both targets healed.
- The selector healing rate is measured by `highhx benchmark` from real trajectories
  ([BENCHMARKS.md](BENCHMARKS.md)).
