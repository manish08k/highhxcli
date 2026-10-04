# Perception

Perception turns what a computer shows into one immutable `ComputerState`. It is read-only: it
never clicks or types. Inside HighhX it runs as the `computer.state` action, so a screen capture
is classified (policy name `screen:capture` when pixels are taken), checked against
`policies.yaml` and audited like any other action.

Code: [`src/highhx/perception/`](../src/highhx/perception) · action:
[`actions/handlers/state.py`](../src/highhx/actions/handlers/state.py).

## ComputerState

| Field | What it holds |
|---|---|
| `surface` | `desktop` · `browser` · `android` |
| `device` | OS, model, serial, screen size, scale (pixels per input point) |
| `active_app`, `active_window`, `windows`, `processes` | Desktop: from the HighhX Computer API. Android: the focused package and activity |
| `cursor`, `focused`, `selected`, `viewport` | Where the pointer is, which element has keyboard focus or is selected, the browser viewport |
| `browser` | URL, title, tabs, whether the page is still loading |
| `elements` | `StateElement`s: role, name, value (never for secret fields), bounds, state, **sources** (`dom`, `ax`, `android`, `ocr`, `vision`), stable attributes (`dom_id`, `testid`, `href`, `resource_id` …), confidence |
| `text`, `ocr_text` | Visible text from the structure, and from OCR (untrusted content) |
| `screenshot` | A reference: size, scale, SHA-256, file path. Pixels are never serialized |
| `perception` | What each source did: `ok`, `unavailable` (with the reason), `failed`, `skipped`, and how long it took |
| `network`, `cwd`, `metadata` | Network hints (page loading), the filesystem context, and the trace ids of the task, session, step, action and execution that took the snapshot |

States are frozen dataclasses with tuples throughout. A new observation is a new state, so the
agent loop and verifiers always compare a *before* and an *after*. `StateElement.from_ui` and
`to_ui` convert losslessly to the per-provider working model (`computer.model.UIElement`).

## Sources (provider interfaces)

| Interface | Built-in adapters |
|---|---|
| `StructureProvider` (`DOMProvider`, `AccessibilityProvider`) | `BrowserDOM` (Chrome DevTools), `DesktopAccessibility` (macOS AX, Windows UIA, Linux AT-SPI through the HighhX Computer API), `AndroidHierarchy` (uiautomator) |
| `ScreenshotProvider` | `BrowserScreenshots` (one image pixel per CSS pixel), `DesktopScreenshots`, `AndroidScreenshots` |
| `OCRProvider` | `TesseractOCRProvider` (local tesseract, when installed) |
| `VisionProvider` / `UIElementDetector` | `ModelVisionProvider`: any `VisionModel` (see [PROVIDERS.md](PROVIDERS.md)) |

All are protocols: tests and plugins inject their own. A source that cannot run is recorded as
`unavailable` with the reason (for example "install tesseract"). Nothing is invented in its place.

## Escalation and cost

`PerceptionEngine.observe(policy, query)` runs the cheapest sources first:

1. structure (DOM / accessibility / Android hierarchy): always, when available
2. a screenshot: only when OCR or vision will run, or when asked for
3. OCR, policy `auto`: when the structure is empty or does not contain the query
4. vision, policy `never` by default. With `auto`, only when the query is still not found after OCR

States are cached for `cache_ttl` seconds and dropped with `invalidate()` (the agent loop
observes again after every action).

## Fusion

`StateFusion` merges the partial views:

- Elements found by two trees (same role and name, overlapping boxes) become one element with both sources.
- OCR and vision boxes are converted from screenshot pixels to input points (divided by the display scale).
- A text line inside a structured element with the same text *corroborates* it (the source is added).
- Any other line becomes a `text` element with the OCR or vision source and its confidence.

Pixels never replace structure: structured elements carry the exact names, states and ids the
executor binds actions to.

## Diffs and tracking

- `VisualDiff` compares two screenshots by a grid of mean luminance, so compression noise does
  not count as a change. It reports the ratio and the changed regions. Images over 4 MP, or
  formats the stdlib decoder (`perception/png.py`) does not read, fall back to digest
  comparison and say so.
- `StateDiff` compares two states structurally: URL, title, focus, application, and the
  elements that appeared, disappeared, moved or changed.
- `ElementTracker` follows a control across observations by identity (role, name, tag, type,
  href, test id), then occurrence, then box.

## CLI

```text
highhx computer state --surface browser|desktop|android [--screenshot] [--ocr auto|always] [--vision [--remote-vision]]
highhx android observe [--screenshot]
```
