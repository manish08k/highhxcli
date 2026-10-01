"""The Chrome DevTools Protocol transport: one WebSocket with flat sessions multiplexed on it.

HighhX connects to the *browser* endpoint once and attaches a session to each tab it works
in (``Target.attachToTarget`` with ``flatten``). Every command and event carries its session
id, so one connection serves every tab, learns at once when a tab is created, closed,
replaced or crashes, and outlives any single page.

Errors say whether a command can have run:

* :class:`BrowserDisconnectedError` — it was never delivered (the connection or the tab's
  session was already gone). Sending it again elsewhere is safe.
* :class:`~highhx.core.errors.OutcomeUnknownError` and its subclasses — it was delivered and
  its answer was lost (timeout, crash, the tab closed, the connection dropped). It may have
  taken effect, so it is never repeated blindly.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import time
from collections.abc import Callable
from typing import Any

from highhx.computer.websocket import WebSocket, WebSocketClosed, WebSocketTimeout
from highhx.core.errors import IntegrationError, OutcomeUnknownError
from highhx.execution.cancellation import CancellationToken

NAVIGATION_STARTED = frozenset(
    {"Page.frameRequestedNavigation", "Page.frameScheduledNavigation", "Page.frameStartedLoading"}
)
_SESSION_GONE = ("session with given id not found", "no session with given id", "target closed")
_TARGET_GONE = ("no target with given id", "target closed")


class BrowserDisconnectedError(IntegrationError):
    """The command was not delivered (the connection or the tab's session was already gone):
    nothing happened, so it is safe to send it again on a new connection or session."""


class BrowserTimeoutError(OutcomeUnknownError):
    """A command got no answer in time; it may or may not have run. (Command ids are unique
    per connection, so a late answer is ignored rather than mistaken for another's.)"""


class PageCrashedError(OutcomeUnknownError):
    """The page's renderer crashed; whatever was running may or may not have taken effect."""


class TargetClosedError(OutcomeUnknownError):
    """The tab closed (or was replaced) while a command was running in it."""


class CDPError(IntegrationError):
    """The browser answered with a protocol error."""

    @property
    def target_gone(self) -> bool:
        """The tab the command addressed no longer exists."""
        return any(gone in self.message.lower() for gone in _TARGET_GONE)


class PageEvents:
    """What one tab's session has reported: loads, the main frame, crashes, dialogs."""

    def __init__(self, target_id: str = "") -> None:
        self.target_id = target_id
        self.navigations = 0
        """Navigations requested/started in any frame since the session opened."""
        self.settled = 0
        """Loads that finished (or same-document navigations) since the session opened."""
        self.loading_frames: set[str] = set()
        self.main_frame = ""
        """The top-level frame's id, once known."""
        self.crashed = False
        self.closed = False
        """The session was detached: the tab closed, was replaced, or the browser went away."""
        self.dialogs: list[dict[str, Any]] = []
        """JavaScript dialogs closed in this tab (drained into the audit trail by the browser)."""

    @property
    def main_frame_loading(self) -> bool:
        """The top-level document is loading (unknown main frame: any frame counts)."""
        if not self.main_frame:
            return bool(self.loading_frames)
        return self.main_frame in self.loading_frames

    def track(self, method: str, params: dict[str, Any]) -> None:
        frame = str(params.get("frameId") or "")
        if method == "Inspector.targetCrashed":
            self.crashed = True
        elif method in NAVIGATION_STARTED:
            self.navigations += 1
        if method == "Page.frameStartedLoading":
            self.loading_frames.add(frame)
        elif method == "Page.frameStoppedLoading":
            self.loading_frames.discard(frame)
            self.settled += 1
        elif method == "Page.navigatedWithinDocument":
            self.settled += 1
        elif method == "Page.frameDetached":
            # an iframe removed while loading never reports frameStoppedLoading
            self.loading_frames.discard(frame)
        elif method == "Page.frameNavigated":
            info = params.get("frame") or {}
            if not info.get("parentId") and info.get("id"):
                self.main_frame = str(info["id"])


def dialog_answer(kind: str) -> bool:
    """HighhX's policy for a JavaScript dialog: an alert is acknowledged; anything that asks for
    a decision (confirm, prompt, "leave page? unsaved changes will be lost") is cancelled — the
    answer that never confirms or discards anything."""
    return kind == "alert"


class CDPConnection:
    """One DevTools WebSocket. Commands for a tab carry its ``session_id``; ``""`` is the
    endpoint itself (the browser, or a page when connected to a page endpoint)."""

    def __init__(self, ws_url: str, *, timeout: float = 30.0, target_id: str = "", allow_remote: bool = False) -> None:
        self.ws = WebSocket(ws_url, timeout=timeout, **({"allow_remote": True} if allow_remote else {}))
        self.timeout = timeout
        self._ids = itertools.count(1)
        self.sessions: dict[str, PageEvents] = {"": PageEvents(target_id)}
        self.events: list[dict[str, Any]] = []
        """Recent events (bounded), for diagnostics."""
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        """Called with every event (after the session's own tracking)."""
        self.last_answer = time.monotonic()
        """When the browser last answered: a connection silent for long is pinged before reuse."""

    # ------------------------------------------------------------ page-level view
    @property
    def page(self) -> PageEvents:
        """The endpoint's own session (a page endpoint's page)."""
        return self.sessions[""]

    @property
    def usable(self) -> bool:
        return not self.ws.closed

    def track_session(self, session_id: str, target_id: str) -> PageEvents:
        events = PageEvents(target_id)
        self.sessions[session_id] = events
        return events

    # ----------------------------------------------------------------- calls
    def send(self, method: str, params: dict[str, Any] | None = None, *, session_id: str = "") -> int:
        """Send without waiting for the answer (its answer is ignored by id)."""
        message_id = next(self._ids)
        payload: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        try:
            self.ws.send(json.dumps(payload))
        except WebSocketClosed:
            raise BrowserDisconnectedError(f"The browser connection was lost before {method} was sent.") from None
        return message_id

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        cancel: CancellationToken | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is not None and session.crashed:
            raise PageCrashedError("The page crashed; it has to be reopened.")
        if session_id and (session is None or session.closed):
            raise BrowserDisconnectedError(f"The tab's session ended before {method} was sent.")
        message_id = self.send(method, params, session_id=session_id)
        # From here on the browser may have acted on the command: a lost answer is an unknown
        # outcome, never a reason to send it again here.
        limit = timeout or self.timeout
        deadline = time.monotonic() + limit
        while True:
            try:
                if time.monotonic() > deadline:  # a steady stream of events must not extend the wait
                    raise WebSocketTimeout("No answer from the browser in time.")
                raw = self.ws.recv(cancel=cancel, deadline=deadline)
            except WebSocketTimeout:
                raise BrowserTimeoutError(
                    f"The browser did not answer {method} within {limit:.0f}s; it may or may not have run.",
                    hint="Observe the page to see what happened; it will not be repeated automatically.",
                ) from None
            except WebSocketClosed:
                raise OutcomeUnknownError(
                    f"The browser connection was lost after {method} was sent; it may or may not have run.",
                    hint="Observe the page to see what happened; it will not be repeated automatically.",
                ) from None
            try:
                message = json.loads(raw)
            except ValueError:
                continue  # not a DevTools message; never let it end the session
            self.last_answer = time.monotonic()
            if message.get("id") == message_id:
                if "error" in message:
                    text = str((message["error"] or {}).get("message") or "")
                    if session_id and any(gone in text.lower() for gone in _SESSION_GONE):
                        # the browser refused it: the session was already gone, nothing ran
                        self._session_ended(session_id)
                        raise BrowserDisconnectedError(f"The tab's session ended before {method} ran.")
                    raise CDPError(f"Browser error in {method}: {text}")
                result: dict[str, Any] = message.get("result") or {}
                return result
            if "method" in message:
                self._dispatch(message)
                if session is not None and session.crashed:
                    raise PageCrashedError(
                        f"The page crashed while {method} was running; it may or may not have run.",
                        hint="HighhX reopens the page on the next action; observe it before deciding again.",
                    )
                if session_id and session is not None and session.closed:
                    raise TargetClosedError(
                        f"The tab closed while {method} was running; it may or may not have run.",
                        hint="HighhX continues in another tab; observe it before deciding again.",
                    )

    def pump(self, *, cancel: CancellationToken | None = None, seconds: float = 0.0) -> None:
        """Process events that already arrived (or arrive within ``seconds``) without a command."""
        poll = 0.02 if seconds else 0.001  # 0: only what has already arrived, without waiting
        deadline = time.monotonic() + max(seconds, poll)
        while True:
            try:
                raw = self.ws.recv(cancel=cancel, deadline=deadline, poll=poll)
            except WebSocketTimeout:
                return
            with contextlib.suppress(ValueError):
                message = json.loads(raw)
                if "method" in message:
                    self._dispatch(message)

    # ---------------------------------------------------------------- events
    def _dispatch(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        session_id = str(message.get("sessionId") or "")
        session = self.sessions.get(session_id)
        if session is not None:
            session.track(method, params)
            if method == "Page.javascriptDialogOpening":
                self._close_dialog(session_id, session, params)
            elif method == "Inspector.detached":
                self._session_ended(session_id)
        if method == "Target.detachedFromTarget":
            self._session_ended(str(params.get("sessionId") or ""))
        self.events.append(message)
        del self.events[:-200]
        for listener in list(self.listeners):
            listener(message)

    def _session_ended(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is not None:
            session.closed = True
        if session_id == "":
            self.ws.close()  # a page endpoint whose page went away (or another client took it)

    def _close_dialog(self, session_id: str, session: PageEvents, params: dict[str, Any]) -> None:
        """A JavaScript dialog blocks every script on the page (and so every HighhX command):
        close it at once with :func:`dialog_answer` and record the decision."""
        kind = str(params.get("type") or "")
        accept = dialog_answer(kind)
        session.dialogs.append(
            {"type": kind, "message": str(params.get("message") or "")[:300], "accepted": accept, "at": time.time()}
        )
        del session.dialogs[:-20]
        with contextlib.suppress(BrowserDisconnectedError):  # fire-and-forget: never nested inside call()
            self.send("Page.handleJavaScriptDialog", {"accept": accept}, session_id=session_id)

    def close(self) -> None:
        self.ws.close()
