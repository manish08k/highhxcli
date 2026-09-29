"""Checking a :class:`~highhx.goals.ir.Condition` against the page as it is now.

Every check reads the structured observation (URL, title, visible text, the accessibility
tree). Only ``media_playing`` asks the page itself — one generic probe of any ``<video>`` or
``<audio>`` element, on any site.
"""

from __future__ import annotations

import re
from typing import Any

from highhx.computer.model import Observation
from highhx.computer.runtime import ComputerRuntime
from highhx.core.errors import HighhXError
from highhx.goals.ir import Condition
from highhx.goals.targets import parse_target

MEDIA_PROBE = """
(() => {
  const media = [...document.querySelectorAll('video, audio')];
  return media.some(m => !m.paused && !m.ended && m.readyState > 2 && m.currentTime > 0);
})()
"""


def media_playing(runtime: ComputerRuntime) -> bool | None:
    """Whether media plays on the page (None: this provider cannot tell)."""
    evaluate = getattr(runtime.provider, "evaluate", None)
    if evaluate is None:
        return None
    try:
        return bool(evaluate(MEDIA_PROBE, cancel=runtime.cancel, retry_safe=True))
    except HighhXError:
        return None


def check(condition: Condition, observation: Observation, runtime: ComputerRuntime | None = None) -> list[str]:
    """What is not (yet) true — empty when the condition holds."""
    problems: list[str] = []
    text = observation.text.lower()
    if condition.url_contains and condition.url_contains not in observation.url:
        problems.append(f"the URL does not contain {condition.url_contains!r} ({observation.url or 'no page'})")
    if condition.title_contains and condition.title_contains.lower() not in observation.title.lower():
        problems.append(f"the title does not contain {condition.title_contains!r} ({observation.title!r})")
    if condition.text and condition.text.lower() not in text:
        problems.append(f"{condition.text!r} is not on the page")
    if condition.text_matches and not re.search(condition.text_matches, observation.text, re.IGNORECASE):
        problems.append(f"nothing on the page matches {condition.text_matches!r}")
    if condition.element and parse_target(condition.element).find(observation) is None:
        problems.append(f"no element matches {condition.element!r}")
    if condition.absent and parse_target(condition.absent).find(observation) is not None:
        problems.append(f"an element matching {condition.absent!r} is still there")
    if condition.media_playing is not None:
        playing = media_playing(runtime) if runtime is not None else None
        if playing is None:
            problems.append("cannot tell whether media is playing here")
        elif playing != condition.media_playing:
            problems.append("no media is playing" if condition.media_playing else "media is still playing")
    if condition.download_completed is not None:
        download: dict[str, Any] = (runtime.last_download if runtime is not None else None) or {}
        done = download.get("state") == "completed"
        if done != condition.download_completed:
            problems.append("no download has completed" if condition.download_completed else "a download completed")
    return problems
