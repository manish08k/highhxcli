"""AgentWorker: one step intent → grounded ActionRequest(s) → the executor.

    StepIntent("click", target={"label": "Submit", "role": "button"})
        │ ground the target on the current state (HybridGrounder; memory hints first)
        ▼
    browser   element found in the DOM/AX → browser.click 'button:"Submit"#1' (bound by the runtime)
              found by OCR/vision only    → browser.click_at x, y (CSS pixels)
    desktop   found by accessibility/OCR  → computer.click_at text=… (re-grounded and window-checked
                                            at click time)
              found by vision/coordinates → computer.click_at x, y
    android                               → android.tap x, y
        │ every request carries the target's label, so the executor rates "Delete" like a
        │ Delete button, whatever the coordinates
        ▼
    protocol.submit → ActionExecutor (classify · policy · approval · run · verify · audit)

A step that needs two actions (type into a field: focus it, then type) submits two requests, the
second a child of the first. Catalog action names pass through unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.actions.protocol import ActionRequest, ActionResponse, submit
from highhx.agent.loop.model import AgentTask, StepIntent
from highhx.grounding import HybridGrounder, Target
from highhx.grounding.hybrid import GroundingResult

if TYPE_CHECKING:
    from highhx.actions.executor import ActionExecutor
    from highhx.perception.state import ComputerState
    from highhx.trajectories.store import TrajectoryStore

GROUNDED_VERBS = frozenset({"click", "double_click", "type", "select"})
STRUCTURED_SOURCES = frozenset({"dom", "ax", "android"})


class GroundingFailed(Exception):
    def __init__(self, result: GroundingResult) -> None:
        super().__init__(result.explain())
        self.result = result


class UnsupportedStep(Exception):
    pass


@dataclass
class WorkResult:
    step: StepIntent
    responses: list[ActionResponse] = field(default_factory=list)
    grounding: GroundingResult | None = None
    requests: list[ActionRequest] = field(default_factory=list)

    @property
    def last(self) -> ActionResponse | None:
        return self.responses[-1] if self.responses else None

    def recorded_target(self) -> dict[str, Any] | None:
        """Every representation of the element that was found (semantic, accessibility, DOM, text,
        visual, coordinate), so a replay can heal through whichever still works."""
        grounding = self.grounding
        if grounding is None or grounding.candidate is None or grounding.candidate.element is None:
            return None
        state = grounding.state
        if state is None:
            return None
        element = grounding.candidate.element
        recorded = bool(self.step.target.get("accessibility") or self.step.target.get("dom"))
        if recorded:  # a replayed target: refresh what drifted and remember the heal
            return grounding.target.healed(element, state, strategy=grounding.strategy).to_dict()
        description = grounding.target.semantic.description if grounding.target.semantic else ""
        return Target.from_element(element, state, description=description).to_dict()


def _browser_selector(element: Any, state: ComputerState) -> str:
    """A selector the browser runtime resolves to exactly this element: role, exact name and its
    occurrence among the elements that selector matches."""
    from highhx.computer.model import Selector

    selector = Selector(element.name, element.role, exact=True)
    matches = [e for e in state.to_observation().elements if selector.matches(e)]
    index = next((i for i, e in enumerate(matches) if e.id == element.id), 0)
    name = element.name.replace('"', "'")
    return f'{element.role}:"{name}"' + (f"#{index + 1}" if len(matches) > 1 else "")


class AgentWorker:
    def __init__(
        self,
        executor: ActionExecutor,
        task: AgentTask,
        surface: str,
        *,
        grounder: HybridGrounder | None = None,
        memory: TrajectoryStore | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.executor = executor
        self.task = task
        self.surface = surface
        self.grounder = grounder or HybridGrounder(emit=executor.events.emit)
        self.memory = memory
        self.trace_id = trace_id

    # ---------------------------------------------------------------- grounding
    def target_for(self, step: StepIntent, state: ComputerState) -> Target:
        """The step's target, enriched with selectors that found the same label before."""
        if step.target.get("accessibility") or step.target.get("dom") or step.target.get("relative"):
            return Target.from_dict(step.target)
        base = Target.of(step.label, str(step.target.get("role") or ""), description=str(step.target.get("description") or ""))
        if self.memory is not None and step.label:
            for hint in self.memory.hints(step.label, url=state.url, app=state.active_app)[:1]:
                remembered = Target.from_dict(hint)
                return Target(
                    base.label,
                    base.role or remembered.role,
                    semantic=base.semantic,
                    accessibility=base.accessibility,
                    dom=remembered.dom,
                    text=base.text,
                    ocr=base.ocr,
                    visual=base.visual,
                    coordinate=remembered.coordinate,
                )
        return base

    def ground(
        self,
        step: StepIntent,
        state: ComputerState,
        *,
        strategies: tuple[str, ...] | None = None,
        escalate: Callable[[str, str], ComputerState | None] | None = None,
    ) -> GroundingResult:
        target = self.target_for(step, state)
        result = self.grounder.ground(state, target, strategies=strategies, escalate=escalate)
        if not result.grounded:
            raise GroundingFailed(result)
        return result

    # ---------------------------------------------------------------- execution
    def execute(
        self,
        step: StepIntent,
        state: ComputerState | None,
        *,
        escalate: Callable[[str, str], ComputerState | None] | None = None,
        strategies: tuple[str, ...] | None = None,
        parent: str | None = None,
    ) -> WorkResult:
        work = WorkResult(step)
        surface = step.surface or self.surface
        grounding = None
        if step.is_verb and step.action in GROUNDED_VERBS and (step.label or step.target.get("relative")):
            if state is None:
                raise UnsupportedStep(f"'{step.action}' needs a screen, but this task has none")
            grounding = self.ground(step, state, strategies=strategies, escalate=escalate)
            work.grounding = grounding
            state = grounding.state or state
            element = grounding.candidate.element if grounding.candidate else None
            if element is not None and element.secret and step.parameters.get("text"):
                # registered before anything is submitted, so no event, log or trace can keep it
                self.executor.app.redactor.add([str(step.parameters["text"])])
        requests = self._requests(step, surface, state, grounding)
        previous = parent
        for request in requests:
            request = ActionRequest.from_dict({**request.to_dict(), "parent_action": previous, "trace_id": self.trace_id})
            work.requests.append(request)
            response = submit(self.executor, request, before=state)  # the verifier checks the step as a whole
            work.responses.append(response)
            previous = request.id
            if not response.result.ok:
                break
        return work

    def _requests(
        self, step: StepIntent, surface: str, state: ComputerState | None, grounding: GroundingResult | None
    ) -> list[ActionRequest]:
        if not step.is_verb:
            name = step.action
            if not self.task.permits(name):
                raise UnsupportedStep(f"{name} is not allowed for this task")
            return [self._request(name, step.parameters, step, grounding)]
        verb = step.action
        params = dict(step.parameters)
        if verb in ("click", "double_click"):
            return [self._click(step, surface, state, grounding, count=2 if verb == "double_click" else 1)]
        if verb == "type":
            text = str(params.get("text", ""))
            if surface == "browser":
                element = grounding.candidate.element if grounding and grounding.candidate else None
                if element is not None and set(element.sources) & STRUCTURED_SOURCES and state is not None:
                    return [self._request("browser.fill", {"target": _browser_selector(element, state), "text": text}, step, grounding)]
                focus = [self._click(step, surface, state, grounding)] if grounding else []
                return [*focus, self._request("browser.insert_text", {"text": text}, step, None)]
            focus = [self._click(step, surface, state, grounding)] if grounding else []
            name = "android.type" if surface == "android" else "computer.type"
            extra = {"device": self.task.device} if surface == "android" and self.task.device else {}
            return [*focus, self._request(name, {"text": text, **extra}, step, None)]
        if verb == "select":
            element = grounding.candidate.element if grounding and grounding.candidate else None
            if surface != "browser" or element is None or state is None:
                raise UnsupportedStep("select works on browser lists")
            return [self._request("browser.select", {"target": _browser_selector(element, state), "option": str(params.get("option", ""))}, step, grounding)]
        if verb == "press":
            key = str(params.get("key") or step.label or "enter")
            name = {"browser": "browser.press", "android": "android.key"}.get(surface, "computer.press")
            return [self._request(name, {"key": key, **self._device(surface)}, step, None)]
        if verb == "scroll":
            direction = str(params.get("direction") or "down")
            if surface == "browser":
                return [self._request("browser.scroll", {"direction": direction}, step, None)]
            if surface == "android":
                return [self._request("android.swipe", {"direction": direction, **self._device(surface)}, step, None)]
            return [self._request("computer.scroll", {"source": "desktop", "direction": direction}, step, None)]
        if verb == "open":
            return [self._request("browser.open", {"url": str(params.get("url") or step.label)}, step, None)]
        if verb == "launch":
            name = str(params.get("name") or params.get("package") or step.label)
            if surface == "android":
                return [self._request("android.launch", {"package": name, **self._device(surface)}, step, None)]
            return [self._request("computer.launch", {"name": name}, step, None)]
        if verb == "back":
            if surface == "android":
                return [self._request("android.back", self._device(surface), step, None)]
            if surface == "browser":
                return [self._request("browser.back", {}, step, None)]
            raise UnsupportedStep("back works in the browser and on Android")
        if verb == "home":
            if surface != "android":
                raise UnsupportedStep("home works on Android")
            return [self._request("android.home", self._device(surface), step, None)]
        if verb == "wait":
            return []
        raise UnsupportedStep(f"unknown verb {verb!r}")

    def _device(self, surface: str) -> dict[str, Any]:
        return {"device": self.task.device} if surface == "android" and self.task.device else {}

    def _click(
        self, step: StepIntent, surface: str, state: ComputerState | None, grounding: GroundingResult | None, *, count: int = 1
    ) -> ActionRequest:
        if grounding is None or grounding.candidate is None:
            point = step.parameters
            if "x" not in point or "y" not in point:
                raise UnsupportedStep("a click needs a target or coordinates")
            x, y = int(point["x"]), int(point["y"])
            element = None
            strategy = "coordinate"
        else:
            element = grounding.candidate.element
            strategy = grounding.strategy
            where = grounding.point
            if where is None:
                raise UnsupportedStep("the target has no position")
            x, y = where
        if surface == "browser":
            if element is not None and set(element.sources) & STRUCTURED_SOURCES and state is not None and count == 1:
                return self._request("browser.click", {"target": _browser_selector(element, state)}, step, grounding)
            if element is not None and set(element.sources) & STRUCTURED_SOURCES and state is not None:
                return self._request("browser.double_click", {"target": _browser_selector(element, state)}, step, grounding)
            return self._request("browser.click_at", {"x": x, "y": y, "count": count}, step, grounding)
        if surface == "android":
            return self._request("android.tap", {"x": x, "y": y, **self._device(surface)}, step, grounding)
        if strategy in ("accessibility", "ocr", "text") and element is not None and element.name and count == 1:
            # re-grounded by name at click time and bound to its window (still there, unmoved, uncovered)
            params: dict[str, Any] = {"text": element.name}
            if self.task.app:
                params["app"] = self.task.app
            return self._request("computer.click_at", params, step, grounding)
        return self._request("computer.click_at", {"x": x, "y": y, "count": count}, step, grounding)

    def _request(self, action: str, params: dict[str, Any], step: StepIntent, grounding: GroundingResult | None) -> ActionRequest:
        target = dict(step.target)
        if grounding is not None and grounding.candidate is not None and grounding.candidate.element is not None:
            target.setdefault("label", grounding.candidate.element.name)
            target.setdefault("role", grounding.candidate.element.role)
        return ActionRequest(
            action_type=action,
            parameters=params,
            intent=step.describe(),
            target=target,
            grounding=grounding.records() if grounding is not None else (),
            environment=self.surface,
            verification=None,
            metadata={"step": step.id, "grounded_by": grounding.strategy if grounding else ""},
            trace_id=self.trace_id,
        )
