"""The computer-use runtime: observe → candidates → safety → execute → observe → verify.

Shared by deterministic automation (Free: the person names the target, e.g.
``click "button:Search"``) and the AI agent (Pro: the model picks one of the
valid :class:`~highhx.computer.model.ActionCandidate` ids). Either way every
action passes the same :class:`~highhx.safety.gate.ActionGate` and is verified by
observing the UI again — an action is never reported as successful just because
it was dispatched.
"""

from __future__ import annotations

import contextlib
import difflib
from dataclasses import dataclass, field
from pathlib import Path
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


EXTENDED_VERBS = {
    "hover": "click",
    "double_click": "click",
    "right_click": "click",
    "download": "click",
    "drag": "click",
    "upload": "type",
}
"""Actions on an element that are valid wherever its base action is (not offered to the AI agent
as candidates, so its choices stay compact; deterministic requests and flows use them)."""

PAGE_ACTIONS = ("back", "forward", "refresh", "new_tab", "close_tab", "switch_tab")


def _extended(candidate_id: str, valid: dict[str, ActionCandidate], observation: Observation) -> ActionCandidate | None:
    verb, _, element_id = candidate_id.partition(":")
    if verb == "press" and element_id:
        # any key or combination the browser can send (only enter/tab/escape are listed, to keep
        # the choices short); "press:backspace" was refused although flows and the CLI offered it
        from highhx.computer.browser import parse_key

        try:
            parse_key(element_id)
        except IntegrationError:
            return None
        return ActionCandidate(candidate_id, "press", None, f"press {element_id}")
    if verb == "scroll" and element_id in ("left", "right"):
        return ActionCandidate(candidate_id, "scroll", None, f"scroll {element_id}")
    base = EXTENDED_VERBS.get(verb)
    if base is None or f"{base}:{element_id}" not in valid:
        return None
    element = observation.element(element_id)
    label = element.label() if element is not None else element_id
    return ActionCandidate(
        candidate_id, verb, element_id, f"{verb.replace('_', ' ')} {label}", needs_text=verb in ("drag", "upload")
    )


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
        self.last_download: dict[str, Any] | None = None
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
        candidate = valid.get(candidate_id) or _extended(candidate_id, valid, observation)
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
        if candidate.verb == "drag" and text is not None:
            # the drop target is an element too: bind it to the live UI the same way
            drop = observation.element(text)
            moved = rebind(drop, live, ordinal(observation, drop)) if drop is not None else None
            if moved is None:
                raise NotFoundError(f"The drop target {text} is no longer on the screen.", hint="Observe again.")
            text = moved.id
        current_action = describe_action(live_candidate, live, tool=self.tool, actor=self.actor, text=text)
        try:
            with self.gate.executing(authorization, current_action) as event:
                try:
                    self._perform(live_candidate, text)
                    after = self._settle_and_observe()
                    verified, problems = self._verify(live_candidate, live, after, text)
                    if live_candidate.verb == "download":
                        # a download leaves the page as it was: it is verified by the finished file
                        done = (self.last_download or {}).get("state") == "completed"
                        verified, problems = done, [] if done else ["the download did not complete"]
                        event.details["download"] = self.last_download
                    if expect is not None:
                        problems += expect.check(after)
                        verified = verified is not False and not problems
                    event.verified = verified
                    if verified is False:
                        event.status = "failed"
                        event.error = "; ".join(problems)
                finally:
                    self._journal(event)
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

    def navigate(self, url: str, *, expect: Expectation | None = None, reuse_tab: bool = False) -> ActionOutcome:
        """Open ``url`` (with ``reuse_tab``: switch to a tab already showing it) and verify where
        the browser really is afterwards — a redirect is reported, a download is followed."""
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
        summary = action.summary
        with self.gate.executing(authorization) as event:
            try:
                result = navigate(url, cancel=self.cancel, **({"reuse_tab": True} if reuse_tab else {}))
                details = result.to_dict() if hasattr(result, "to_dict") else None
                if details is not None:
                    event.details["navigation"] = details
                download = (details or {}).get("download")
                if download:
                    # the URL was a file: it was downloaded, not opened — verified by the finished file
                    after = self.observe()
                    event.verified = download.get("state") == "completed"
                    summary = f"Downloaded {download.get('path') or download.get('filename')}"
                    return ActionOutcome(f"navigate:{url}", True, event.verified, summary, after, [])
                after = self._settle_and_observe()
                problems = [] if _same_page(url, after.url) else [f"the browser is at {after.url}, not {url}"]
                if expect is not None:
                    problems += expect.check(after)
                if problems and getattr(self.provider, "last_wait_settled", True) is False:
                    problems.append("the page was still loading when HighhX stopped waiting")
                if details and details.get("reused_tab"):
                    summary += " (it was already open)"
                elif details and details.get("new_tab"):
                    summary += " in a new tab"
                event.verified = not problems
                if problems:
                    event.status, event.error = "failed", "; ".join(problems)
            finally:
                self._journal(event)
        return ActionOutcome(f"navigate:{url}", not problems, not problems, summary, after, problems)

    def page_action(self, op: str, value: str | None = None) -> ActionOutcome:
        """Tab and history actions — back, forward, refresh, new_tab (optionally at a URL),
        close_tab, switch_tab (to a tab id or text in its URL/title) — through the safety gate,
        then verified against what the browser shows."""
        if op not in PAGE_ACTIONS:
            raise UsageError(f"Unknown page action {op!r} (expected: {', '.join(PAGE_ACTIONS)}).")
        method = getattr(
            self.provider, {"back": "go_back", "forward": "go_forward", "refresh": "reload"}.get(op, op), None
        )
        if method is None:
            raise UsageError(f"{self.provider.name} cannot {op.replace('_', ' ')}.")
        if op == "new_tab" and value and not urlparse(value).scheme:
            value = f"https://{value}"
        label = {
            "back": "Go back",
            "forward": "Go forward",
            "refresh": "Reload the page",
            "new_tab": f"Open a new tab{f' at {value}' if value else ''}",
            "close_tab": "Close the tab",
            "switch_tab": f"Switch to the tab {value!r}",
        }[op]
        before = self.observation.url if self.observation is not None else ""
        action = ActionDescriptor(
            ActionKind.NAVIGATE,
            label,
            self.tool,
            target=value or before or op,
            application=self.provider.name,
            actor=self.actor,
            attributes=attrs(op=op, host=urlparse(value or "").hostname or ""),
        )
        authorization = self.gate.authorize(action, policy_action=f"computer:{op}", grant=f"computer:{op}")
        with self.gate.executing(authorization) as event:
            try:
                args = [value] if op in ("new_tab", "switch_tab") and value else []
                result = method(*args, cancel=self.cancel)
                after = self._settle_and_observe()
                problems: list[str] = []
                expected = ""
                if op in ("back", "forward") and isinstance(result, str):
                    expected = result
                elif op == "new_tab" and value:
                    expected = value
                elif op == "switch_tab" and isinstance(result, dict):
                    expected = str(result.get("url") or "")
                if expected and not _same_page(expected, after.url):
                    problems.append(f"the browser is at {after.url}, not {expected}")
                event.verified = not problems
                if problems:
                    event.status, event.error = "failed", "; ".join(problems)
            finally:
                self._journal(event)
        return ActionOutcome(op, not problems, not problems, label, after, problems)

    def _journal(self, event: Any) -> None:
        """Attach what the browser went through during the action (recoveries, tab changes,
        dialogs, downloads) to its audit record."""
        drain = getattr(self.provider, "drain_journal", None)
        entries = drain() if drain is not None else []
        if entries:
            event.details["browser"] = entries
            from highhx.actions.events import BROWSER_JOURNAL

            bus = getattr(getattr(getattr(self.gate, "engine", None), "ctx", None), "events", None)
            for entry in entries:
                name = BROWSER_JOURNAL.get(str(entry.get("event")))
                if bus is not None and name is not None:
                    with contextlib.suppress(Exception):
                        bus.emit(
                            name,
                            kind=entry.get("event"),
                            detail=str(entry.get("detail") or "")[:300],
                            state=entry.get("state"),
                        )

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
            elif candidate.verb in EXTENDED_VERBS:
                self._perform_extended(candidate.verb, element, text)
        except ElementNotFoundError as exc:
            raise NotFoundError(exc.message, hint="The UI changed; observe again.") from None

    def _perform_extended(self, verb: str, element: str, text: str | None) -> None:
        method = getattr(self.provider, verb, None)
        if method is None:
            raise UsageError(f"{self.provider.name} cannot {verb.replace('_', ' ')}.")
        if verb == "drag":
            method(element, text or "", cancel=self.cancel)
        elif verb == "upload":
            method(element, [p for p in (text or "").split("\n") if p], cancel=self.cancel)
        elif verb == "download":
            self.last_download = None
            download = method(element, cancel=self.cancel)
            self.last_download = download.to_dict() if hasattr(download, "to_dict") else None
        else:
            method(element, cancel=self.cancel)

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
        if candidate.verb in ("scroll", "hover"):
            return None, []
        if candidate.verb == "press" and not changed and not _edits(candidate.id.split(":", 1)[1]):
            return None, []  # a caret, focus or copy key may change nothing visible: unverified, not failed
        if candidate.verb == "upload" and same is not None:
            names = [Path(p).name for p in (text or "").split("\n") if p]
            ok = bool(names) and same.value.replace("\\", "/").rsplit("/", 1)[-1] == names[0]
            return ok, [] if ok else ["the file field does not show the chosen file"]
        return (True, []) if changed else (False, ["no visible change after the action"])


def _edits(key: str) -> bool:
    """Whether pressing ``key`` changes content when it works: Enter, the delete keys and plain
    characters do; navigation keys, Escape, Tab and shortcuts may change nothing that is seen."""
    from highhx.computer.browser import parse_key

    try:
        modifiers, base = parse_key(key)
    except IntegrationError:
        return True
    if set(modifiers) - {"shift"}:
        return False
    return base in ("enter", "return", "backspace", "delete", "forwarddelete", "space") or len(base) == 1


def _same_page(requested: str, actual: str) -> bool:
    """The browser ended up where it was sent (a ``www.`` redirect, either way, counts)."""
    a, b = urlparse(requested), urlparse(actual)

    def host(url: Any) -> str:
        name = (url.hostname or "").lower()
        return name.removeprefix("www.")

    return host(a) == host(b) and (b.path or "/").startswith((a.path or "/").rstrip("/") or "/")


def _selector_text(selector: Selector) -> str:
    return (
        f"{selector.role + ' ' if selector.role else ''}{selector.name!r}"
        if selector.name
        else (selector.role or "anything")
    )
