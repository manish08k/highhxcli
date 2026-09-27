"""Computer use: semantic UI automation shared by HighhX Free and Pro.

* Free — deterministic automation with known targets: ``highhx computer …``, flow
  files, and ``highhx do`` for plain requests that map to fixed actions.
* Pro — the AI agent observes the UI and chooses among the valid action candidates.

Both go through the same runtime (:mod:`~highhx.computer.runtime`) and the same
safety gate. Perception is accessibility-first: native accessibility (macOS),
browser DOM (Chromium family via DevTools), local OCR (tesseract); a vision model
is an optional plugin, never required.
"""
