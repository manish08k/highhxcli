"""Deterministic intents: plain-language requests HighhX can carry out without AI.

``highhx do "run the tests"`` or ``highhx do "open chrome and search for Adele"``
map to a known command or a known automation step by fixed rules — no model,
no network call, the same result every time. Anything that needs understanding
(``"fix whatever is failing"``) is not matched and is left to the Pro agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus

DEFAULT_SEARCH_URL = "https://www.google.com/search?q={query}"
BROWSERS = ("chrome", "google chrome", "chromium", "edge", "brave", "browser", "the browser", "my browser")

# Whole-request patterns → (HighhX command argv). Anchored: extra words mean it is not deterministic.
_COMMANDS: tuple[tuple[str, list[str]], ...] = (
    (r"(?:run|execute|start)?\s*(?:the|all(?: the)?|my)?\s*(?:unit\s+)?tests?", ["test"]),
    (r"(?:run\s+)?(?:the\s+)?(?:checks?|lint(?:ing)?|linters?|quality checks?)", ["check"]),
    (r"(?:run\s+(?:the\s+)?)?build(?: the project| it)?", ["build"]),
    (r"(?:show\s+)?(?:the\s+)?(?:project\s+)?status", ["status"]),
    (r"(?:show\s+)?(?:project\s+)?info(?:rmation)?", ["info"]),
    (r"(?:run\s+)?(?:the\s+)?doctor|check (?:my|the) (?:setup|environment)", ["doctor"]),
    (r"(?:run\s+)?diagnose|diagnose (?:the )?(?:project|setup)", ["diagnose"]),
    (r"start(?: the)? dev(?:elopment)?(?: server)?|run(?: the)? dev(?: server)?", ["dev"]),
    (r"start(?: all| the)?(?: services)?", ["start"]),
    (r"stop(?: all| the)?(?: services)?", ["stop"]),
    (r"restart(?: all| the)?(?: services)?", ["restart"]),
    (r"(?:run\s+(?:a\s+)?)?security(?: scan| check)?|scan for (?:secrets|security issues)", ["security"]),
    (r"(?:show\s+)?(?:the\s+)?(?:recent\s+)?history", ["history"]),
    (r"(?:show\s+)?(?:the\s+)?(?:latest\s+)?logs?", ["logs"]),
    (r"(?:show\s+)?(?:the\s+)?git status", ["git", "status"]),
    (r"install(?: the)? dependencies|install deps", ["deps", "install"]),
    (r"(?:list|show)(?: the)? outdated(?: dependencies| packages)?", ["deps", "outdated"]),
    (r"(?:run\s+)?(?:the\s+)?(?:auto(?:matic)?\s*)?fix(?:es)?|format(?: the)? code", ["fix"]),
)
_URL = re.compile(r"^(?:https?://\S+|(?:localhost|127\.0\.0\.1)(?::\d+)?(?:/\S*)?|[\w-]+(?:\.[\w-]+)+(?:/\S*)?)$", re.I)


@dataclass(frozen=True)
class Intent:
    kind: str
    """command | navigate | search | launch"""
    argv: list[str] = field(default_factory=list)
    url: str = ""
    app: str = ""
    description: str = ""


def _normalise(text: str) -> str:
    text = " ".join(text.strip().split())
    text = re.sub(r"^(?:please|pls|can you|could you|highhx,?)\s+", "", text, flags=re.I)
    return text.rstrip(".!?").strip()


def parse(request: str, *, search_url: str = DEFAULT_SEARCH_URL) -> Intent | None:
    text = _normalise(request)
    low = text.lower()
    for pattern, argv in _COMMANDS:
        if re.fullmatch(pattern, low):
            return Intent("command", argv=list(argv), description=f"highhx {' '.join(argv)}")
    # "open chrome and search for adele", "search the web for adele", "google adele"
    match = re.fullmatch(
        r"(?:open|launch|start|use)\s+(?P<app>[\w .]+?)\s+and\s+(?:search|look up|google)(?:\s+for)?\s+(?P<q>.+)",
        text,
        re.I,
    ) or re.fullmatch(r"(?:search(?:\s+the\s+web)?|google|look up)(?:\s+for)?\s+(?P<q>.+)", text, re.I)
    if match:
        app = (match.groupdict().get("app") or "browser").strip().lower()
        if app in BROWSERS:
            query = match.group("q").strip().strip("\"'")
            url = search_url.format(query=quote_plus(query))
            return Intent("search", url=url, app=app, description=f"search for {query!r} in the browser")
        return None
    match = re.fullmatch(
        r"(?:open|go to|navigate to|visit|browse to)\s+(?P<target>\S+)(?:\s+in\s+(?P<app>[\w ]+))?", text, re.I
    )
    if match and _URL.match(match.group("target")):
        target = match.group("target")
        url = (
            target
            if re.match(r"^https?://", target, re.I)
            else (f"http://{target}" if target.lower().startswith(("localhost", "127.0.0.1")) else f"https://{target}")
        )
        return Intent("navigate", url=url, description=f"open {url} in the browser")
    match = re.fullmatch(r"(?:open|launch|start)\s+(?:the\s+)?(?P<app>[\w .+-]+?)(?:\s+app)?", text, re.I)
    if match:
        app = match.group("app").strip()
        words = app.lower().split()
        # A follow-up clause ("… and check whether …") or a long description needs understanding.
        if "and" in words or len(words) > 3 or app.lower() in ("tests", "dev server", "services", "the project"):
            return None
        return Intent("launch", app=app, description=f"launch {app}")
    return None
