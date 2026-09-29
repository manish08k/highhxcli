"""Verification strategies: named checks of the state an action was meant to produce.

Each plan step names a strategy (``page_open``, ``media_playing``, ``file_exists`` …). After the
step runs, the strategy looks at the action's result — and, where it can, at the world (the
file system) — and answers one of:

    verified     the expected state was observed
    failed       the expected state was not observed: the step failed, whatever the action said
    unverified   HighhX cannot observe it (a keystroke's effect, a page in Safari): reported as such

A step whose action "succeeded" but whose verification failed is a failed step. Nothing is
reported as done because an attempt was made.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

VERIFIED, FAILED, UNVERIFIED = "verified", "failed", "unverified"


@dataclass(frozen=True)
class Check:
    status: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAILED

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class Evidence:
    """What a strategy may look at."""

    action: str
    inputs: dict[str, Any]
    ok: bool
    verified: bool | None
    output: dict[str, Any]
    error: str
    root: Path | None


Strategy = Callable[[Evidence], Check]
STRATEGIES: dict[str, Strategy] = {}
DESCRIPTIONS: dict[str, str] = {}


def strategy(name: str, description: str) -> Callable[[Strategy], Strategy]:
    def register(fn: Strategy) -> Strategy:
        STRATEGIES[name] = fn
        DESCRIPTIONS[name] = description
        return fn

    return register


def _from_action(e: Evidence, what: str) -> Check:
    """The action's own verification (it re-observed after acting)."""
    if e.verified is True:
        return Check(VERIFIED, what)
    if e.verified is False:
        return Check(FAILED, e.error or f"not observed: {what}")
    return Check(UNVERIFIED, f"not observable: {what}")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


@strategy("page_open", "The HighhX browser shows the expected site.")
def page_open(e: Evidence) -> Check:
    expected, landed = str(e.inputs.get("url") or ""), str(e.output.get("url") or "")
    if e.inputs.get("app") and not landed:
        return Check(UNVERIFIED, f"opened in {e.inputs['app']}, which HighhX cannot read")
    if not landed:
        return Check(FAILED, e.error or "no page is open")
    if _host(expected) and _host(expected) != _host(landed):
        return Check(FAILED, f"the browser is at {landed}, not {expected}")
    title = str(e.output.get("title") or "")
    return Check(VERIFIED, f"{title or _host(landed)} is open")


@strategy("search_results", "The site's search results page is showing for the query.")
def search_results(e: Evidence) -> Check:
    if e.inputs.get("app") and not e.output.get("url"):
        return Check(UNVERIFIED, f"searched in {e.inputs['app']}, which HighhX cannot read")
    return _from_action(e, f"results for {e.inputs.get('query')!r} are showing")


@strategy("media_playing", "A media element on the opened result is playing.")
def media_playing(e: Evidence) -> Check:
    if e.output.get("playing") is True:
        return Check(VERIFIED, f"playing {e.output.get('title') or 'the result'}")
    return Check(FAILED, e.error or "the media is not playing")


@strategy("app_running", "The application is running after the launch.")
def app_running(e: Evidence) -> Check:
    return _from_action(e, f"{e.inputs.get('name') or 'the application'} is running")


@strategy("app_frontmost", "The application is the frontmost application.")
def app_frontmost(e: Evidence) -> Check:
    if e.output.get("frontmost") is True:
        return Check(VERIFIED, f"{e.output.get('app') or e.inputs.get('app')} is in front")
    return Check(FAILED, e.error or f"{e.inputs.get('app')} is not in front")


@strategy("file_exists", "The file or folder exists afterwards.")
def file_exists(e: Evidence) -> Check:
    rel = str(e.output.get("path") or e.inputs.get("path") or "")
    if e.root is None or not rel:
        return _from_action(e, f"{rel or 'the path'} exists")
    path = e.root / rel
    wanted_folder = str(e.output.get("kind") or e.inputs.get("kind") or "") == "folder"
    if path.is_dir() if wanted_folder else path.is_file():
        return Check(VERIFIED, f"{rel} exists")
    return Check(FAILED, f"{rel} does not exist")


@strategy("opened_by_os", "The operating system accepted the request to open it.")
def opened_by_os(e: Evidence) -> Check:
    if not e.ok:
        return Check(FAILED, e.error or "it could not be opened")
    return Check(UNVERIFIED, f"handed to {e.output.get('with') or e.output.get('app') or 'the system'}")


@strategy("process_exit", "The command exited successfully and its result was captured.")
def process_exit(e: Evidence) -> Check:
    if not e.ok:
        return Check(FAILED, e.error or "the command failed")
    code = e.output.get("exit_code", e.output.get("returncode"))
    return Check(VERIFIED, f"exit code {code}" if code is not None else "completed")


@strategy("command_captured", "The command ran and its output was captured.")
def command_captured(e: Evidence) -> Check:
    if not e.ok:
        return Check(FAILED, e.error or "the command failed")
    return Check(VERIFIED, "result captured") if e.output else Check(UNVERIFIED, "no output to check")


@strategy("listing", "The folder's entries were read.")
def listing(e: Evidence) -> Check:
    if not e.ok:
        return Check(FAILED, e.error or "the folder could not be read")
    return Check(VERIFIED, f"{len(e.output.get('entries') or [])} item(s)")


@strategy("files_found", "Every file the search returned exists.")
def files_found(e: Evidence) -> Check:
    if not e.ok:
        return Check(FAILED, e.error or "the search failed")
    files = [str(f.get("path", "")) for f in e.output.get("files") or []]
    if e.root is not None:
        gone = [f for f in files if not (e.root / f).is_file()]
        if gone:
            return Check(FAILED, f"{gone[0]} no longer exists")
    return Check(VERIFIED, f"{len(files)} file(s)" if files else "no matching files")


@strategy("element_clicked", "The UI changed as expected after the click.")
def element_clicked(e: Evidence) -> Check:
    return _from_action(e, "the click took effect")


@strategy("keys_sent", "Keystrokes were delivered to the frontmost application.")
def keys_sent(e: Evidence) -> Check:
    if not e.ok:
        return Check(FAILED, e.error or "the keys were not sent")
    return Check(
        UNVERIFIED, f"sent to {e.output.get('app') or 'the frontmost application'}; the effect is not observable"
    )


@strategy("none", "Nothing to verify.")
def nothing(e: Evidence) -> Check:
    if not e.ok:
        return Check(FAILED, e.error or "failed")
    return _from_action(e, "done") if e.verified is not None else Check(UNVERIFIED, "")


def verify(name: str, evidence: Evidence) -> Check:
    """Run strategy ``name`` (an unknown name is a bug: it fails loudly rather than passing)."""
    fn = STRATEGIES.get(name)
    if fn is None:
        return Check(FAILED, f"unknown verification strategy {name!r}")
    if not evidence.ok and name not in ("media_playing",):
        return Check(FAILED, evidence.error or "the action failed")
    return fn(evidence)


def overall(checks: list[Check]) -> str:
    """A run's verification: failed if any step failed, verified if every step was, else partial."""
    if not checks:
        return UNVERIFIED
    if any(c.failed for c in checks):
        return FAILED
    if all(c.status == VERIFIED for c in checks):
        return VERIFIED
    return "partially verified" if any(c.status == VERIFIED for c in checks) else UNVERIFIED
