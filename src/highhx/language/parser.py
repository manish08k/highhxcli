"""Normalisation and clause splitting.

    "Please open YouTube, then play lofi."  →  "open YouTube, then play lofi"
                                             →  ["open YouTube", "play lofi"]

A conjunction ("and", "then", "and then", "after that", ",", ";") splits a request only where
the next part starts with a known verb — so a query such as "rock and roll" stays one piece.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

CONJUNCTION = re.compile(r"\s*(?:,?\s*and\s+then|,?\s*after\s+that,?|,?\s*then|\s+and|;|,)\s+", re.I)
POLITE = re.compile(
    r"^(?:please\s+|pls\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|hey\s+|ok\s+|highhx,?\s+|i\s+want\s+to\s+|let'?s\s+|go\s+ahead\s+and\s+)+",
    re.I,
)


def normalise(text: str) -> str:
    """Trim politeness, trailing punctuation and extra spaces (the meaning is unchanged)."""
    text = " ".join(text.strip().split())
    text = POLITE.sub("", text)
    return text.rstrip(".!?").strip()


def split_clauses(text: str, verbs: Iterable[str]) -> list[str]:
    """Split ``text`` at conjunctions followed by one of ``verbs``."""
    words = {w.lower() for w in verbs}
    pieces: list[str] = []
    start = 0
    for match in CONJUNCTION.finditer(text):
        rest = text[match.end() :].lstrip()
        first = rest.split(" ", 1)[0].lower() if rest else ""
        if first in words:
            pieces.append(text[start : match.start()].strip())
            start = match.end()
    pieces.append(text[start:].strip())
    return [p for p in pieces if p]
