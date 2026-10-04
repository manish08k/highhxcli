# Grounding and selectors

Grounding answers "which element (or point) is *the Submit button* on this screen?" It is
read-only: the result becomes a parameter of an `ActionRequest`, and the executor decides
whether that action may run.

Code: [`src/highhx/grounding/`](../src/highhx/grounding).

## One target, many representations

A `Target` keeps every representation it was recorded with, so a change to one does not lose the control:

| Representation | Survives | Breaks on |
|---|---|---|
| `semantic` (label, role, description) | everything that keeps the meaning | a different control |
| `accessibility` (role, accessible name, occurrence *index of count*) | CSS and DOM restructuring | a renamed label |
| `dom` (`id`, `data-testid`/`data-test`/`data-cy`, `name`, `href`, stable class tokens, Android `resource_id`) | label and layout changes | renamed attributes |
| `text` (visible text, fuzzy) | role changes, small rewordings | larger rewordings |
| `ocr` (text read from pixels) | missing structure (canvases, remote desktops) | low-contrast text |
| `visual` (description for a vision model, last box, screenshot digest) | almost anything visible | needs a vision model |
| `relative` (anchor label + direction: right, left, above, below, near; role) | unlabeled controls next to a label | the anchor renamed or moved apart |
| `coordinate` (point + viewport + app + URL) | nothing structural, so it is the last resort | any layout change |

Generated class names (`css-1x2y3z`, `sc-…`, `jsx-…`, hashes) are not used as selectors.
`Target.from_element` captures every representation of an element when recording.
`Target.healed` refreshes the representations that drifted and remembers each heal.
`Target.parse` reads the selector syntax people type (`button:Save`, `link:"Docs"#2`).

## HybridGrounder

```text
accessibility (1.00) → dom (0.95) → text (0.85) → relative (0.75) → ocr (0.70) → vision (0.65) → coordinate (0.30)
```

- **score** = strategy weight × candidate confidence, plus 0.15 when an earlier strategy pointed
  at the same element. Weights are configurable per task (`weights=`), and so is the order
  (`strategies=`).
- **Stop** at the first confident (≥ 0.5 by default) and unambiguous answer.
- **Ties are never guessed.** The grounder tries to break them with the representations the
  tied strategy did not use: DOM attributes, the recorded box, the recorded point, and the
  recorded occurrence index (only while the number of matches is unchanged). If they still
  tie, the result is `ambiguous` with the candidates.
- **Escalation on demand.** When OCR or vision is needed and the state lacks it, `escalate(level,
  query)` fetches a richer observation (the `computer.state` action again, so it is still
  policy-checked and audited). Vision runs only when the task allows it.
- **Every attempt is recorded**: `{strategy, result: success|failed|ambiguous|unavailable|skipped,
  candidates, best, detail, seconds}`. Attempts are emitted as `grounding.attempt` events,
  stored with each trajectory step, and shown by the trace and the live dashboard.
- **Coordinates alone are not trusted by default.** Their score (0.3 × 0.5) is below the
  minimum, so a task has to lower `min_score` explicitly, and the recorded URL, app and
  viewport must match.

## Vision grounding

`VisionGrounder` asks a `VisionModel` (see [PROVIDERS.md](PROVIDERS.md)) where the target is in
the state's screenshot, read from memory or from the capture file `computer.state` saved. The
box is converted to input points and bound to the structured element under it when that is
plausibly the same thing. The model's JSON is validated (clamped to the image, positive size).
A vision answer is a candidate like any other and cannot change an action's risk: the executor
rates a click by its target's *label* too (see [SECURITY.md](SECURITY.md#computer-use)).

## Memory hints

The agent loop's worker asks trajectory memory for selectors that grounded the same label
before (same page or app first) and merges their DOM and coordinate representations into the
target. A control found once by its test id is found the same way next time.

## CLI

```text
highhx computer ground "button:Export" --surface browser     # every attempt, the point, never acts
highhx android find "Save"                                     # several matches are listed, never guessed
```
