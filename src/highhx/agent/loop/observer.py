"""AgentObserver: what the screen shows now, through the ``computer.state`` action.

Observation is an action like any other: classified (a screenshot is ``screen:capture``),
policy-checked and audited. The observer asks for pixels only when grounding needs them
(:meth:`escalate`), so most steps cost one structured read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from highhx.agent.loop.model import AgentTask

if TYPE_CHECKING:
    from highhx.actions.executor import ActionExecutor
    from highhx.perception.state import ComputerState


class ObservationError(Exception):
    def __init__(self, message: str, status: str = "failed") -> None:
        super().__init__(message)
        self.status = status


class AgentObserver:
    def __init__(self, executor: ActionExecutor, task: AgentTask, surface: str) -> None:
        self.executor = executor
        self.task = task
        self.surface = surface
        self.last: ComputerState | None = None
        self.count = 0

    @property
    def active(self) -> bool:
        return self.surface in ("desktop", "browser", "android")

    def _inputs(self, **extra: Any) -> dict[str, Any]:
        inputs: dict[str, Any] = {"surface": self.surface, "ocr": "never" if self.surface == "browser" else "auto"}
        if self.task.device and self.surface == "android":
            inputs["device"] = self.task.device
        if self.task.app and self.surface == "desktop":
            inputs["app"] = self.task.app
        return {**inputs, **extra}

    def observe(self, **extra: Any) -> ComputerState | None:
        if not self.active:
            return None
        from highhx.actions.handlers.state import state_from_result

        result = self.executor.run("computer.state", self._inputs(**extra))
        if not result.ok:
            raise ObservationError(result.error or "the screen could not be observed", result.status)
        state = state_from_result(result)
        self.count += 1
        self.last = state
        return state

    def escalate(self, level: str, query: str) -> ComputerState | None:
        """A richer observation for grounding: ``ocr`` or ``vision`` (only when the task allows it)."""
        if not self.active:
            return None
        if level == "vision":
            if self.task.vision == "never":
                return None
            return self.observe(screenshot=True, vision="auto", query=query, remote_vision=self.task.remote_vision)
        if self.task.ocr == "never":
            return None
        return self.observe(screenshot=True, ocr="always", query=query)
