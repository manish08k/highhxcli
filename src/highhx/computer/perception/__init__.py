"""Perception: from what is on the screen to a point to act on — optional, never required.

    ground(driver, "Save")      1. the accessibility tree: an element with that name and bounds
                                2. OCR of a screenshot (tesseract), when installed and allowed
                                → Grounded(point, source, …) or GroundingError (none, or several)

Core computer control (observe → act → verify) needs none of this: it works on accessibility
elements. Perception is the fallback for surfaces without a structured UI (canvases, remote
desktops, images of text). No model is bundled — a vision model can be plugged in through
:class:`~highhx.computer.providers.VisionProvider`.
"""

from highhx.computer.perception.grounding import Grounded, GroundingError, ground

__all__ = ["Grounded", "GroundingError", "ground"]
