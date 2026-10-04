"""Tool routing: which kind of tool a task needs, cheapest reliable first. Deterministic, no model.

    "Calculate the average price from prices.csv"   → filesystem + code     (no screen at all)
    "Click the export button"                       → browser
    "Remove the background in Photoshop"            → desktop + vision      (a canvas: no structure)
    "Run the tests"                                 → shell
    "Create a contact on the Android phone"         → android

Each route has a cost (structured tools are cheaper and more reliable than screens, screens read
by a vision model are the most expensive) plus how much it can touch (risk). Past success on a
tool (trajectory memory) breaks ties. Unavailable tools (no adb, no browser) are ranked last with
the reason. Routing only chooses a surface: every action still goes through the executor. The agent loop uses the first route's surface unless the task names
one.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

TOOLS = ("filesystem", "shell", "code", "api", "browser", "desktop", "android", "sandbox")
COST = {"filesystem": 1, "shell": 1, "code": 2, "api": 1, "sandbox": 2, "browser": 3, "desktop": 4, "android": 4}
SURFACE = {"browser": "browser", "desktop": "desktop", "android": "android"}

RULES: dict[str, tuple[str, ...]] = {
    "android": (r"\bandroid\b", r"\b(?:phone|tablet|emulator)\b", r"\bapk\b", r"\bmobile app\b", r"\bon (?:my|the) device\b"),
    "browser": (
        r"https?://",
        r"\b(?:web ?site|web ?page|webpage|browser|chrome|safari|firefox|url|tab)\b",
        r"\b(?:log ?in|sign ?in|checkout|cart|search (?:for|on))\b",
        r"\b(?:button|link|form|dropdown|page)\b",
        r"\b(?:gmail|github|google|amazon|youtube|linkedin|jira|notion)\b",
    ),
    "desktop": (
        r"\b(?:photoshop|illustrator|figma|excel|word|powerpoint|keynote|numbers|pages|textedit|finder|"
        r"calculator|preview|system settings|system preferences|terminal app|vs ?code|xcode|slack app)\b",
        r"\b(?:desktop|window|menu bar|dock|application|native app)\b",
    ),
    "shell": (r"\b(?:run|execute) (?:the )?(?:tests?|build|lint|script|command)\b", r"\b(?:pytest|npm|yarn|pnpm|make|cargo|go test|pip|docker|git)\b", r"\b(?:install|compile|deploy|lint)\b"),
    "code": (r"\b(?:fix|refactor|implement|debug)\b", r"\b(?:bug|function|class|module|code|script)\b"),
    "filesystem": (r"\.(?:csv|json|ya?ml|txt|md|log|xlsx?)\b", r"\b(?:csv|file|folder|directory|rename|copy|move)\b", r"\b(?:average|sum|count|total) .* from\b"),
    "api": (r"\b(?:api|endpoint|rest|graphql|webhook|http request|curl)\b",),
    "sandbox": (r"\b(?:sandbox|isolated|untrusted|safely try|throwaway)\b",),
}
_COMPILED = {tool: tuple(re.compile(p, re.I) for p in patterns) for tool, patterns in RULES.items()}
CANVAS_APPS = re.compile(r"\b(?:photoshop|illustrator|figma|sketch|blender|game|canvas|remote desktop|vnc|citrix|chart|map)\b", re.I)


@dataclass(frozen=True)
class Route:
    tool: str
    score: float
    cost: int
    reason: str
    available: bool = True
    needs_vision: bool = False

    @property
    def surface(self) -> str:
        return SURFACE.get(self.tool, "none")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "surface": self.surface,
            "score": round(self.score, 2),
            "cost": self.cost,
            "reason": self.reason,
            "available": self.available,
            "needs_vision": self.needs_vision,
        }


RISK = {"filesystem": 1, "shell": 2, "code": 2, "api": 2, "sandbox": 0, "browser": 2, "desktop": 3, "android": 3}
"""How much a route can touch outside the project (a sandbox the least, a real desktop the most)."""


@dataclass
class ToolRouter:
    availability: Mapping[str, Callable[[], tuple[bool, str]]] = field(default_factory=dict)
    """tool → () -> (available, why not). Tools without a check count as available."""
    history: Mapping[str, float] = field(default_factory=dict)
    """tool → success rate of past tasks on it (from trajectory memory)."""

    def route(self, goal: str) -> list[Route]:
        routes: list[Route] = []
        for tool in TOOLS:
            hits = [p.pattern for p in _COMPILED[tool] if p.search(goal)]
            if not hits:
                continue
            check = self.availability.get(tool)
            available, why = check() if check else (True, "")
            needs_vision = tool == "desktop" and bool(CANVAS_APPS.search(goal))
            score = float(len(hits))
            reason = f"matches {len(hits)} {tool} cue(s)"
            if tool in self.history:
                rate = self.history[tool]
                score += 0.5 * (rate - 0.5)  # a tool that usually works here is preferred among equals
                reason += f"; {rate:.0%} of past tasks succeeded"
            if not available:
                reason += f"; unavailable: {why}"
            routes.append(Route(tool, score, COST[tool] + (2 if needs_vision else 0) + RISK[tool] // 2, reason, available, needs_vision))
        if not routes:
            routes.append(Route("shell", 0.1, COST["shell"], "no specific cue: start with commands and files"))
        # most relevant first; among equally relevant, the cheapest; unavailable ones last
        return sorted(routes, key=lambda r: (not r.available, -r.score, r.cost))

    def surface(self, goal: str) -> str:
        """The surface for the first available screen route, or ``none`` when the task needs no screen."""
        for route in self.route(goal):
            if not route.available:
                continue
            return route.surface
        return "none"

    def needs_specialists(self, goal: str) -> bool:
        """Several distinct kinds of tool are needed (e.g. research on the web, then edit code)."""
        kinds = {r.surface if r.surface != "none" else "work" for r in self.route(goal) if r.available and r.score >= 1.0}
        return len(kinds) >= 2


def success_history(store: object) -> dict[str, float]:
    """Success rate per tool from recent trajectories (surface → tool)."""
    totals: dict[str, list[int]] = {}
    for trajectory in getattr(store, "recent", lambda **_: [])(limit=200):
        tool = {"browser": "browser", "desktop": "desktop", "android": "android"}.get(trajectory.surface, "shell")
        done = totals.setdefault(tool, [0, 0])
        done[0] += trajectory.status == "completed"
        done[1] += 1
    return {tool: ok / total for tool, (ok, total) in totals.items() if total >= 3}


def default_router(store: object = None) -> ToolRouter:
    def adb() -> tuple[bool, str]:
        from highhx.drivers.android.adb import find_adb

        return (True, "") if find_adb() else (False, "adb is not installed")

    def browser() -> tuple[bool, str]:
        from highhx.computer.browser import find_browser

        return (True, "") if find_browser() else (False, "no Chromium-family browser was found")

    return ToolRouter({"android": adb, "browser": browser}, success_history(store) if store is not None else {})
