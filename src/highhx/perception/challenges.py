"""Human-verification challenges (CAPTCHAs): detected, never solved.

A CAPTCHA asks whether a person is present. HighhX answers honestly: when one is on the screen,
the agent loop stops with ``needs_user`` and the person solves it, then resumes the task
(``highhx agent --resume task_…``). HighhX does not click through, solve, outsource or evade
challenges, and adds no stealth or fingerprint evasion to look like a person.

Detection is deliberately specific (provider endpoints and the providers' own wording) so an
ordinary page that mentions "robots" does not stop a task.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from highhx.perception.state import ComputerState

PROVIDER_URLS = (
    "google.com/recaptcha",
    "recaptcha.net/recaptcha",
    "hcaptcha.com",
    "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform",
    "arkoselabs.com",
    "funcaptcha.com",
    "geo.captcha-delivery.com",
)
PHRASES = re.compile(
    r"\bi'?m not a robot\b"
    r"|\bverify (?:that )?you are (?:a )?human\b"
    r"|\bverifying you are human\b"
    r"|\bare you a robot\b"
    r"|\bcomplete the security check\b"
    r"|\bpress (?:&|and) hold\b"
    r"|\bunusual traffic from your computer network\b"
    r"|\bselect all (?:images|squares) with\b"
    r"|\bplease solve (?:this|the) captcha\b",
    re.IGNORECASE,
)
NAMES = re.compile(r"\b(?:re)?captcha\b|\bhcaptcha\b|\bturnstile\b|\bcloudflare challenge\b", re.IGNORECASE)


@dataclass(frozen=True)
class Challenge:
    kind: str
    """``captcha``."""
    evidence: str
    """What gave it away (a provider endpoint, a phrase, a control's name) — short, page-derived."""


def detect(state: ComputerState | None) -> Challenge | None:
    if state is None:
        return None
    url = state.browser.url if state.browser is not None else ""
    for endpoint in PROVIDER_URLS:
        if endpoint in url:
            return Challenge("captcha", f"the page is {endpoint}")
    for element in state.elements:
        for _key, value in element.attributes:
            if any(endpoint in value for endpoint in PROVIDER_URLS):
                return Challenge("captcha", f"a frame from {next(e for e in PROVIDER_URLS if e in value)}")
        if (
            element.name
            and NAMES.search(element.name)
            and element.role in ("iframe", "frame", "checkbox", "group", "region", "dialog")
        ):
            return Challenge("captcha", f"{element.role} {element.name[:40]!r}")
    for text in (state.text, state.ocr_text, state.browser.title if state.browser is not None else ""):
        match = PHRASES.search(text or "")
        if match:
            return Challenge("captcha", f"the screen says {match.group(0)!r}")
    return None
