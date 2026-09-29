"""From a request to Task IR.

HighhX Free: the deterministic resolver (:mod:`highhx.language.grammar`) understands the request;
its plan is translated into generic primitives here. What a site needs comes from the target
registry's *data* (its address, search URL, what a result link looks like) — there is no
per-site code, and a site that is not in the registry is simply a URL.

HighhX Pro: a request nobody programmed goes to the model planner
(:meth:`~highhx.goals.planner.ModelPlanner.understand`), which writes the IR — validated exactly
like one written by hand.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus, urlparse

from highhx.actions.resolver import Resolution, ResolverContext, Step
from highhx.goals.ir import Action, Condition, TaskIR
from highhx.language.targets import DEFAULT_SEARCH, TargetRegistry

PLAY_WAIT = 20.0


class NotBrowserTask(Exception):
    """The request resolves, but not to browser actions (run it as a normal HighhX request)."""

    def __init__(self, actions: list[str]) -> None:
        super().__init__(", ".join(actions))
        self.actions = actions


def _search(step: Step, registry: TargetRegistry) -> list[Action]:
    query = str(step.inputs["query"])
    site = registry.site(str(step.inputs["site"])) if step.inputs.get("site") else None
    if site is not None and site.search:
        url = site.search_url(query)
        marker = site.results or ""
        where = site.name
    else:
        url, marker, where = DEFAULT_SEARCH.format(query=quote_plus(query)), "/search?q=", "the web"
    return [
        Action(
            "browser.navigate",
            f"show {where}'s results for {query!r}",
            value=url,
            expected_result=f"results for {query!r} are showing",
            expect=Condition(url_contains=marker) if marker else None,
        )
    ]


def _results_marker(template: str) -> Condition | None:
    """What a results URL built from ``template`` always contains ("/results?search_query=")."""
    prefix = urlparse(template.split("{", 1)[0])
    marker = prefix.path + (f"?{prefix.query}" if prefix.query else "")
    return Condition(url_contains=marker) if marker.strip("/") else None


def _play(step: Step, registry: TargetRegistry) -> list[Action]:
    query = str(step.inputs["query"])
    site = registry.site(str(step.inputs.get("site") or "youtube"))
    if site is None or site.play is None:
        raise NotBrowserTask([step.action])
    play = site.play
    return [
        Action(
            "browser.navigate",
            f"show {site.name}'s results for {query!r}",
            value=play.results.format(query=quote_plus(query)),
            expected_result="results are showing",
            expect=_results_marker(play.results),
        ),
        Action(
            "browser.click",
            "open the first result",
            target=f"link[href*={play.result_href}]#1",
            expected_result="the first result is open",
            expect=Condition(url_contains=play.watch_url),
            timeout=15.0,
        ),
        Action(
            "browser.verify",
            "the media must actually play",
            expected_result="the media is playing",
            expect=Condition(media_playing=True),
            timeout=PLAY_WAIT,
        ),
    ]


def _translate(step: Step, registry: TargetRegistry) -> list[Action]:
    name, inputs = step.action, step.inputs
    if inputs.get("app"):  # a browser HighhX cannot drive (Safari …): not something the loop can verify
        raise NotBrowserTask([name])
    what = step.description or name
    if name in ("browser.open", "browser.navigate"):
        return [
            Action("browser.navigate", what, value=str(inputs["url"]), expected_result=f"{what} — the page is open")
        ]
    if name == "browser.new_tab":
        return [Action("browser.new_tab", what, value=str(inputs["url"]) if inputs.get("url") else None)]
    if name == "browser.search":
        return _search(step, registry)
    if name == "browser.play":
        return _play(step, registry)
    if name == "browser.click":
        return [Action("browser.click", what, target=str(inputs["target"]))]
    if name in ("browser.fill", "browser.type") and "text" in inputs:
        return [Action("browser.type", what, target=str(inputs["target"]), value=str(inputs["text"]))]
    if name == "browser.press":
        return [Action("browser.press", what, value=str(inputs["key"]).lower())]
    if name in ("browser.back", "browser.forward", "browser.close_tab", "browser.screenshot"):
        return [Action(name, what)]
    if name in ("browser.refresh", "browser.reload"):
        return [Action("browser.recover", what)]
    if name == "browser.switch_tab":
        return [Action("browser.switch_tab", what, target=str(inputs.get("target") or inputs.get("tab") or ""))]
    raise NotBrowserTask([name])


def from_resolution(resolution: Resolution, registry: TargetRegistry) -> TaskIR:
    """The deterministic plan as Task IR (browser actions only)."""
    foreign = [s.action for s in resolution.steps if not s.action.startswith("browser.")]
    if foreign:
        raise NotBrowserTask(foreign)
    steps: list[Action] = []
    for step in resolution.steps:
        for action in _translate(step, registry):
            if steps and steps[-1].signature() == action.signature():
                continue  # "play X" resolves to search + play: the results page is opened once
            steps.append(action)
    context: dict[str, str] = {}
    first = next((a.value for a in steps if a.primitive == "navigate" and a.value), None)
    if first:
        context["start_url"] = first
    success: tuple[Condition, ...] = ()
    last = steps[-1] if steps else None
    if last is not None and last.primitive == "verify" and last.expect is not None:
        success = (last.expect,)
    return TaskIR(
        goal=resolution.text, context=context, steps=tuple(steps), success_conditions=success, allow_replanning=True
    )


def deterministic(request: str, context: ResolverContext) -> TaskIR | None:
    """Task IR for ``request`` without AI, or None when the resolver does not understand it."""
    from highhx.decision.deterministic import DeterministicDecider

    decision = DeterministicDecider(context).decide(request)
    if decision.resolution is None:
        return None
    return from_resolution(decision.resolution, context.targets())


def describe(task: TaskIR) -> dict[str, Any]:
    return task.to_dict()
