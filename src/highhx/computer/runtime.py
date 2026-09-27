"""The computer-use runtime: observe → candidates → safety → execute → observe → verify.

Shared by deterministic automation (Free: the person names the target, e.g.
``click "button:Search"``) and the AI agent (Pro: the model picks one of the
valid :class:`~highhx.computer.model.ActionCandidate` ids). Either way every
action passes the same :class:`~highhx.safety.gate.ActionGate` and is verified by
observing the UI again — an action is never reported as successful just because
it was dispatched.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from highhx.computer.browser import ElementNotFoundError
from highhx.computer.model import (
    ActionCandidate,
    Observation,
    Selector,
    UIElement,
    candidates,
    describe_action,
    ordinal,
    rebind,
)
from highhx.computer.providers import ComputerUseProvider
from highhx.core.errors import (
    IntegrationError,
    NotFoundError,
    OperationCancelledError,
    OutcomeUnknownError,
    UsageError,
    ValidationError,
)
from highhx.execution.cancellation import CancellationToken
from highhx.safety.actions import ActionDescriptor, ActionKind, Actor, attrs
from highhx.safety.gate import ActionGate


class InvalidActionError(ValidationError):
    """The requested action is not one of the valid candidates for the current UI."""


@dataclass
class Expectation:
    """Explicit post-conditions for deterministic flows."""

    text: str | None = None
    url_contains: str | None = None
    title_contains: str | None = None
    element: Selector | None = None
    absent: Selector | None = None

    def check(self, observation: Observation) -> list[str]:
        problems = []
        if self.text and self.text.lower() not in observation.text.lower():
            problems.append(f"text {self.text!r} is not on the screen")
        if self.url_contains and self.url_contains not in observation.url:
            problems.append(f"the URL does not contain {self.url_contains!r} ({observation.url})")
        if self.title_contains and self.title_contains.lower() not in observation.title.lower():
            problems.append(f"the title does not contain {self.title_contains!r} ({observation.title!r})")
        if self.element and not any(self.element.matches(e) for e in observation.elements):
            problems.append(f"no element matches {self.element}")
        if self.absent and any(self.absent.matches(e) for e in observation.elements):
            problems.append(f"an element matching {self.absent} is still present")
        return problems


@dataclass
class ActionOutcome:
    action: str
    ok: bool
    verified: bool | None
    summary: str
    observation: Observation | None = None
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "ok": self.ok,
            "verified": self.verified,
            "summary": self.summary,
            "problems": self.problems,
            "url": self.observation.url if self.observation else None,
            "title": self.observation.title if self.observation else None,
        }


class ComputerRuntime:
    def __init__(
        self,
        provider: ComputerUseProvider,
        gate: ActionGate,
        *,
        actor: Actor,
        tool: str = "computer",
        cancel: CancellationToken | None = None,
        settle: float = 0.3,
    ) -> None:
        self.provider = provider
        self.gate = gate
        self.actor = actor
        self.tool = tool
        self.cancel = cancel or CancellationToken()
        self.settle = settle
        self.observation: Observation | None = None
        self._failed_sensitive: set[str] = set()

    # ---------------------------------------------------------------- observe
    def observe(self) -> Observation:
        self.observation = self.provider.observe(cancel=self.cancel)
        return self.observation

    def candidates(self) -> list[ActionCandidate]:
        return candidates(self.observation or self.observe())

    def resolve(self, selector: Selector, *, role_hint: frozenset[str] | None = None) -> UIElement:
        """Deterministically find the element a person named (no AI involved)."""
        observation = self.observation or self.observe()
        matches = [
            e for e in observation.elements if selector.matches(e) and (role_hint is None or e.role in role_hint)
        ]
        if not matches and not selector.exact and selector.name:
            exact = Selector(selector.name, selector.role, exact=True)
            matches = [e for e in observation.elements if exact.matches(e)]
        if len(matches) <= selector.index:
            names = [e.name for e in observation.elements if e.name]
            close = difflib.get_close_matches(selector.name or "", names, n=5, cutoff=0.5)
            hint = (
                f"Did you mean: {', '.join(repr(c) for c in close)}?"
                if close
                else "Run `highhx computer observe` to list elements."
            )
            raise NotFoundError(
                f"No element matches {_selector_text(selector)} in {observation.application}.", hint=hint
            )
        return matches[selector.index]

    # ---------------------------------------------------------------- execute
    def act(self, candidate_id: str, text: str | None = None, *, expect: Expectation | None = None) -> ActionOutcome:
        observation = self.observation or self.observe()
        valid = {c.id: c for c in candidates(observation)}
        candidate = valid.get(candidate_id)
        if candidate is None:
            raise InvalidActionError(
                f"'{candidate_id}' is not a valid action in the current UI.",
                hint="Observe again and choose one of the listed action ids.",
            )
        if candidate.verb in ("done", "ask_user"):
            return ActionOutcome(candidate_id, True, None, candidate.description, observation)
        if candidate.needs_text and text is None:
            raise UsageError(f"'{candidate_id}' needs text.")
        planned = describe_action(candidate, observation, tool=self.tool, actor=self.actor, text=text)
        if planned.digest in self._failed_sensitive:
            raise IntegrationError(
                f"Not retrying automatically: {planned.summary} already failed or its outcome is unknown.",
                hint="Re-observe the UI and decide again, or ask the user.",
            )
        verdict = self.gate.classify(planned)
        authorization = self.gate.authorize(
            planned, policy_action=f"computer:{candidate.verb}", grant=f"computer:{candidate.verb}"
        )
        # Re-observe right before acting and bind the approval to the element as it is *now*.
        live = self.provider.observe(cancel=self.cancel)
        element = observation.element(candidate.element) if candidate.element else None
        live_candidate = candidate
        if element is not None:
            current = rebind(element, live, ordinal(observation, element))
            if current is None:
                raise NotFoundError(
                    f"{element.label()} is no longer on the screen; the UI changed.",
                    hint="Observe again and choose a new action.",
                )
            live_candidate = ActionCandidate(
                candidate.id.replace(candidate.element or "", current.id),
                candidate.verb,
                current.id,
                candidate.description,
                candidate.needs_text,
            )
        current_action = describe_action(live_candidate, live, tool=self.tool, actor=self.actor, text=text)
        try:
            with self.gate.executing(authorization, current_action) as event:
                self._perform(live_candidate, text)
                after = self._settle_and_observe()
                verified, problems = self._verify(live_candidate, live, after, text)
                if expect is not None:
                    problems += expect.check(after)
                    verified = verified is not False and not problems
                event.verified = verified
                if verified is False:
                    event.status = "failed"
                    event.error = "; ".join(problems)
        except OutcomeUnknownError:
            # It may have happened (e.g. the browser connection dropped after the click was sent).
            # Never repeat it automatically — not even after the browser reconnects — and make the
            # next decision start from a fresh observation.
            self._failed_sensitive.add(planned.digest)
            self._failed_sensitive.add(current_action.digest)
            self.observation = None
            raise
        except Exception:
            if verdict.requires_confirmation:
                self._failed_sensitive.add(planned.digest)
            self.observation = None
            raise
        if verified is False and verdict.requires_confirmation:
            self._failed_sensitive.add(planned.digest)
        summary = current_action.summary + ("" if verified is not False else f" — not verified: {'; '.join(problems)}")
        return ActionOutcome(candidate.id, verified is not False, verified, summary, after, problems)

    def navigate(self, url: str, *, expect: Expectation | None = None) -> ActionOutcome:
        navigate = getattr(self.provider, "navigate", None)
        if navigate is None:
            raise UsageError(f"{self.provider.name} cannot open URLs.")
        parsed = urlparse(url)
        if not parsed.scheme:
            url = f"https://{url}"
            parsed = urlparse(url)
        action = ActionDescriptor(
            ActionKind.NAVIGATE,
            f"Open {url}",
            self.tool,
            target=url,
            application=self.provider.name,
            actor=self.actor,
            attributes=attrs(host=parsed.hostname or ""),
        )
        authorization = self.gate.authorize(action, policy_action="computer:navigate", grant="computer:navigate")
        with self.gate.executing(authorization) as event:
            navigate(url, cancel=self.cancel)
            after = self._settle_and_observe()
            problems = [] if _same_page(url, after.url) else [f"the browser is at {after.url}, not {url}"]
            if expect is not None:
                problems += expect.check(after)
            event.verified = not problems
            if problems:
                event.status, event.error = "failed", "; ".join(problems)
        return ActionOutcome(f"navigate:{url}", not problems, not problems, action.summary, after, problems)

    # ---------------------------------------------------------------- helpers
    def _perform(self, candidate: ActionCandidate, text: str | None) -> None:
        p = self.provider
        element = candidate.element or ""
        try:
            if candidate.verb == "click":
                p.click(element, cancel=self.cancel)
            elif candidate.verb == "focus":
                p.click(element, cancel=self.cancel)  # clicking a field focuses it
            elif candidate.verb == "type":
                p.type_text(element, text or "", cancel=self.cancel)
            elif candidate.verb == "select":
                p.select(element, text or "", cancel=self.cancel)
            elif candidate.verb == "press":
                p.press(candidate.id.split(":", 1)[1], cancel=self.cancel)
            elif candidate.verb == "scroll":
                p.scroll(candidate.id.split(":", 1)[1], cancel=self.cancel)
        except ElementNotFoundError as exc:
            raise NotFoundError(exc.message, hint="The UI changed; observe again.") from None

    def _settle_and_observe(self) -> Observation:
        if self.cancel.wait(self.settle):
            raise OperationCancelledError("Computer-use action cancelled.")
        wait_ready = getattr(self.provider, "wait_ready", None)
        if wait_ready is not None:
            wait_ready(cancel=self.cancel)
        return self.observe()

    @staticmethod
    def _verify(
        candidate: ActionCandidate, before: Observation, after: Observation, text: str | None
    ) -> tuple[bool | None, list[str]]:
        element = before.element(candidate.element) if candidate.element else None
        same = rebind(element, after, ordinal(before, element)) if element is not None else None
        changed = before.fingerprint() != after.fingerprint()
        if candidate.verb == "type" and element is not None:
            if element.secret:
                return None, []  # a secret value cannot (and must not) be read back
            if same is None:
                return (True, []) if changed else (False, ["the field disappeared"])
            ok = same.value == (text or "")
            return ok, [] if ok else [f"the field contains {same.value[:60]!r}, expected {text!r}"]
        if candidate.verb == "select" and same is not None:
            ok = same.value == text or changed
            return ok, [] if ok else ["the selection did not change"]
        if candidate.verb == "click" and element is not None and element.checked is not None and same is not None:
            ok = same.checked != element.checked
            return ok, [] if ok else ["the control did not toggle"]
        if candidate.verb == "focus":
            return (same is not None and same.focused) or None, []
        if candidate.verb == "scroll":
            return None, []
        return (True, []) if changed else (False, ["no visible change after the action"])


def _same_page(requested: str, actual: str) -> bool:
    a, b = urlparse(requested), urlparse(actual)
    return (a.hostname or "") == (b.hostname or "") and (b.path or "/").startswith((a.path or "/").rstrip("/") or "/")


def _selector_text(selector: Selector) -> str:
    return (
        f"{selector.role + ' ' if selector.role else ''}{selector.name!r}"
        if selector.name
        else (selector.role or "anything")
    )
