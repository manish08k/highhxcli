"""Hybrid grounding: from "the Submit button" to the one element (or point) it is, by whichever
representation still works. Read-only: a grounding result becomes an ``ActionRequest``
parameter, and the executor decides whether that action may run. See docs/GROUNDING.md."""

from highhx.grounding.grounders import (
    AccessibilityGrounder,
    Candidate,
    CoordinateGrounder,
    DOMGrounder,
    OCRGrounder,
    RelativeGrounder,
    TextGrounder,
    VisionGrounder,
)
from highhx.grounding.hybrid import ORDER, WEIGHTS, GroundingAttempt, GroundingResult, HybridGrounder
from highhx.grounding.selectors import (
    AccessibilitySelector,
    CoordinateSelector,
    DOMSelector,
    OCRSelector,
    RelativeSelector,
    SemanticSelector,
    Target,
    TextSelector,
    VisualSelector,
)

__all__ = [
    "ORDER",
    "WEIGHTS",
    "AccessibilityGrounder",
    "AccessibilitySelector",
    "Candidate",
    "CoordinateGrounder",
    "CoordinateSelector",
    "DOMGrounder",
    "DOMSelector",
    "GroundingAttempt",
    "GroundingResult",
    "HybridGrounder",
    "OCRGrounder",
    "OCRSelector",
    "RelativeGrounder",
    "RelativeSelector",
    "SemanticSelector",
    "Target",
    "TextGrounder",
    "TextSelector",
    "VisionGrounder",
    "VisualSelector",
]
