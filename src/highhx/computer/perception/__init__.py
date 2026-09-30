"""Perception: from what is on the screen to a point to act on — optional, never required.

    ground(driver, "Save")      1. the accessibility tree: an element with that name and bounds
                                2. OCR of a screenshot (tesseract), when installed and allowed
                                → Grounded(point, source, …, window) or GroundingError (none, or several)
    still_there(driver, found)  None, or why the window it was found in moved, closed or was covered

Core computer control (observe → act → verify) needs none of this: it works on accessibility
elements. Perception is the fallback for surfaces without a structured UI (canvases, remote
desktops, images of text). No model is bundled — a vision model can be plugged in through
:class:`~highhx.computer.providers.VisionProvider`.
"""

from highhx.computer.perception.grounding import Grounded, GroundingError, ground, still_there

__all__ = ["Grounded", "GroundingError", "ground", "still_there"]
