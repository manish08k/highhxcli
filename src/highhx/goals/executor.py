"""Executing one Task IR action on the page — always through the computer runtime.

Each primitive maps onto :class:`~highhx.computer.runtime.ComputerRuntime`, so every action is
classified by the safety policy, approved when it must be (sensitive controls, uploads, the
agent typing into secret fields is refused), executed, verified against a fresh observation,
and written to the audit log. This module adds only what the IR needs on top: generic targets
(:mod:`highhx.goals.targets`) re-observed until they appear, and the action's ``expect``
condition polled until it holds or the action's timeout ends.
"""

from __future__ import annotations

import inspect
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from highhx.computer.model import Observation, UIElement
from highhx.computer.runtime import ActionOutcome, ComputerRuntime
from highhx.core.errors import NotFoundError, OperationCancelledError, UsageError
from highhx.goals import conditions
from highhx.goals.ir import Action, Condition
from highhx.goals.targets import parse_target

MAX_READ = 4000
"""Characters of page text ``read`` keeps (page text is untrusted data, never instructions)."""


@dataclass
class StepResult:
    ok: bool
    verified: bool | None
    summary: str
    problems: list[str] = field(default_factory=list)
    observation: Observation | None = None
    output: dict[str, Any] = field(default_factory=dict)


def web_address(url: str) -> str:
    return url if urlparse(url).scheme else f"https://{url}"


def _accepts(method: Any, name: str) -> bool:
    try:
        return name in inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False


class GoalExecutor:
    def __init__(self, runtime: ComputerRuntime, *, screenshots: Path | None = None, poll: float = 0.25) -> None:
        self.runtime = runtime
        self.screenshots = screenshots
        self.poll = poll

    # ------------------------------------------------------------- observing
    def observe(self) -> Observation:
        return self.runtime.observe()

    def tab(self) -> str:
        return str(getattr(self.runtime.provider, "_target_id", "") or "")

    def health(self) -> tuple[int, str]:
        """(reconnections so far, the working tab) — a change across an action means the browser
        recovered underneath it (a lost connection, a restarted browser, a closed tab)."""
        return int(getattr(self.runtime.provider, "reconnects", 0) or 0), self.tab()

    def _sleep(self, seconds: float) -> None:
        if self.runtime.cancel.wait(seconds):
            raise OperationCancelledError("The task was cancelled.")

    def resolve(self, target: str, timeout: float) -> UIElement:
        """The element ``target`` names, re-observing until it appears (slow pages) or ``timeout``."""
        parsed = parse_target(target)
        deadline = time.monotonic() + timeout
        while True:
            observation = self.observe()
            element = parsed.find(observation)
            if element is not None:
                return element
            if time.monotonic() >= deadline:
                import difflib

                names = [e.name for e in observation.elements if e.name]
                close = difflib.get_close_matches(
                    parsed.selector.name if parsed.selector and parsed.selector.name else target, names, n=5, cutoff=0.5
                )
                raise NotFoundError(
                    f"No element matches {target!r} on {observation.title or observation.url or 'the page'}.",
                    hint=f"Similar: {', '.join(repr(c) for c in close)}" if close else "Observe the page again.",
                )
            self._sleep(self.poll)

    def holds(self, condition: Condition, timeout: float) -> tuple[list[str], Observation]:
        """Poll ``condition`` until it holds or ``timeout``: (what is still not true, the last page)."""
        deadline = time.monotonic() + timeout
        while True:
            observation = self.observe()
            problems = conditions.check(condition, observation, self.runtime)
            if not problems or time.monotonic() >= deadline:
                return problems, observation
            self._sleep(self.poll)

    # ------------------------------------------------------------- executing
    def run(self, action: Action, timeout: float) -> StepResult:
        """Execute ``action``; errors (not found, denied, lost connection …) propagate to the loop."""
        timeout = action.timeout if action.timeout is not None else timeout
        handler = getattr(self, f"_{action.primitive}")
        result: StepResult = handler(action, timeout)
        if action.expect is not None and result.ok and action.primitive not in ("verify", "wait"):
            problems, observation = self.holds(action.expect, timeout)
            result.observation = observation
            if problems:
                result.ok, result.verified = False, False
                result.problems += problems
            else:
                result.verified = True
        return result

    @staticmethod
    def _from(outcome: ActionOutcome, **output: Any) -> StepResult:
        return StepResult(
            outcome.ok, outcome.verified, outcome.summary, list(outcome.problems), outcome.observation, output
        )

    def _navigate(self, action: Action, timeout: float) -> StepResult:
        rt = self.runtime
        url = web_address(str(action.value))
        navigate = getattr(rt.provider, "navigate", None)
        reuse = navigate is not None and _accepts(navigate, "reuse_tab")
        return self._from(rt.navigate(url, reuse_tab=reuse), url=url)

    def _new_tab(self, action: Action, timeout: float) -> StepResult:
        url = web_address(action.value) if action.value else None
        return self._from(self.runtime.page_action("new_tab", url))

    def _close_tab(self, action: Action, timeout: float) -> StepResult:
        return self._from(self.runtime.page_action("close_tab"))

    def _switch_tab(self, action: Action, timeout: float) -> StepResult:
        return self._from(self.runtime.page_action("switch_tab", str(action.target)))

    def _back(self, action: Action, timeout: float) -> StepResult:
        return self._from(self.runtime.page_action("back"))

    def _forward(self, action: Action, timeout: float) -> StepResult:
        return self._from(self.runtime.page_action("forward"))

    def _recover(self, action: Action, timeout: float) -> StepResult:
        """Reload the page and look again (a stale or half-loaded page)."""
        return self._from(self.runtime.page_action("refresh"))

    def _observe(self, action: Action, timeout: float) -> StepResult:
        observation = self.observe()
        return StepResult(
            True,
            None,
            f"{observation.title or observation.url or observation.application}",
            observation=observation,
            output={"elements": len(observation.elements)},
        )

    def _find(self, action: Action, timeout: float) -> StepResult:
        element = self.resolve(str(action.target), timeout)
        return StepResult(
            True,
            True,
            f"found {element.label()}",
            observation=self.runtime.observation,
            output={"element": element.id, "label": element.label()},
        )

    def _element_action(self, verb: str, action: Action, timeout: float, text: str | None = None) -> StepResult:
        element = self.resolve(str(action.target), timeout)
        outcome = self.runtime.act(f"{verb}:{element.id}", text)
        return self._from(outcome, element=element.label())

    def _click(self, action: Action, timeout: float) -> StepResult:
        return self._element_action("click", action, timeout)

    def _type(self, action: Action, timeout: float) -> StepResult:
        return self._element_action("type", action, timeout, str(action.value))

    def _select(self, action: Action, timeout: float) -> StepResult:
        return self._element_action("select", action, timeout, str(action.value))

    def _download(self, action: Action, timeout: float) -> StepResult:
        result = self._element_action("download", action, timeout)
        result.output["download"] = self.runtime.last_download
        return result

    def _upload(self, action: Action, timeout: float) -> StepResult:
        files = [str(Path(f.strip()).expanduser().resolve()) for f in str(action.value).splitlines() if f.strip()]
        missing = [f for f in files if not Path(f).is_file()]
        if not files or missing:
            raise UsageError(f"No such file: {', '.join(missing) or '(none given)'}")
        return self._element_action("upload", action, timeout, "\n".join(files))

    def _press(self, action: Action, timeout: float) -> StepResult:
        self.observe()
        return self._from(self.runtime.act(f"press:{str(action.value).lower()}"))

    def _scroll(self, action: Action, timeout: float) -> StepResult:
        self.observe()
        return self._from(self.runtime.act(f"scroll:{str(action.value).lower()}"))

    def _read(self, action: Action, timeout: float) -> StepResult:
        if action.target:
            element = self.resolve(action.target, timeout)
            text = " ".join(p for p in (element.name, element.value) if p)
            where = element.label()
        else:
            observation = self.observe()
            text, where = observation.text, observation.title or observation.url
        output: dict[str, Any] = {"text": text[:MAX_READ]}
        if action.value:
            matches = list(dict.fromkeys(m.group(0) for m in re.finditer(action.value, text, re.IGNORECASE)))[:20]
            output["matches"] = matches
            found = bool(matches)
            summary = f"found {', '.join(matches[:5])}" if found else f"nothing matches {action.value!r} in {where}"
            return StepResult(found, found, summary, [] if found else [summary], self.runtime.observation, output)
        return StepResult(
            True, None, f"read {len(text)} characters from {where}", observation=self.runtime.observation, output=output
        )

    def _wait(self, action: Action, timeout: float) -> StepResult:
        if action.expect is not None:
            limit = float(action.value) if action.value is not None else timeout
            problems, observation = self.holds(action.expect, limit)
            summary = f"waited until {action.expect.describe()}" if not problems else "the page did not get there"
            return StepResult(not problems, not problems, summary, problems, observation)
        seconds = float(action.value or 0)
        self._sleep(seconds)
        return StepResult(True, None, f"waited {seconds:g}s", observation=self.runtime.observation)

    def _screenshot(self, action: Action, timeout: float) -> StepResult:
        capture = getattr(self.runtime.provider, "screenshot", None)
        if capture is None:
            raise UsageError(f"{self.runtime.provider.name} cannot take screenshots.")
        data: bytes = capture(cancel=self.runtime.cancel)
        output: dict[str, Any] = {"bytes": len(data)}
        if self.screenshots is not None:
            self.screenshots.mkdir(parents=True, exist_ok=True)
            path = self.screenshots / f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}-{time.monotonic_ns() % 10_000}.png"
            path.write_bytes(data)
            output["path"] = str(path)
        summary = f"screenshot saved to {output['path']}" if "path" in output else "screenshot taken"
        return StepResult(bool(data), bool(data), summary, observation=self.runtime.observation, output=output)

    def _verify(self, action: Action, timeout: float) -> StepResult:
        assert action.expect is not None
        problems, observation = self.holds(action.expect, timeout)
        summary = f"{action.expect.describe()}" + (" holds" if not problems else " does not hold")
        return StepResult(not problems, not problems, summary, problems, observation)
