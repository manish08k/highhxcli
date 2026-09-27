"""Agent session lifecycle (shared by the CLI and the platform).

    created ──▶ running ◀──▶ waiting_for_confirmation
                  │  ▲
                  ▼  │ (next request)
      completed / failed / cancelled ──▶ closed ──▶ running (resumed)

``completed``, ``failed`` and ``cancelled`` describe the latest request; the
session can run again after any of them. ``closed`` means no process is attached;
resuming the session (``--continue`` / ``--resume``) reopens it.
"""

from __future__ import annotations

from enum import StrEnum


class SessionState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLOSED = "closed"


TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.CREATED: frozenset({SessionState.RUNNING, SessionState.CLOSED}),
    SessionState.RUNNING: frozenset(
        {SessionState.WAITING_FOR_CONFIRMATION, SessionState.COMPLETED, SessionState.FAILED, SessionState.CANCELLED}
    ),
    SessionState.WAITING_FOR_CONFIRMATION: frozenset(
        {SessionState.RUNNING, SessionState.FAILED, SessionState.CANCELLED}
    ),
    SessionState.COMPLETED: frozenset({SessionState.RUNNING, SessionState.CLOSED}),
    SessionState.FAILED: frozenset({SessionState.RUNNING, SessionState.CLOSED}),
    SessionState.CANCELLED: frozenset({SessionState.RUNNING, SessionState.CLOSED}),
    SessionState.CLOSED: frozenset({SessionState.RUNNING}),
}

LEGACY = {"active": SessionState.RUNNING, "interrupted": SessionState.CANCELLED}
"""Status names used by HighhX 0.2.0."""


class InvalidTransitionError(ValueError):
    def __init__(self, current: SessionState, target: SessionState) -> None:
        super().__init__(f"invalid session transition {current} → {target}")
        self.current = current
        self.target = target


def parse_state(value: str) -> SessionState:
    if value in LEGACY:
        return LEGACY[value]
    return SessionState(value)


def can_transition(current: SessionState, target: SessionState) -> bool:
    return current == target or target in TRANSITIONS[current]


def check_transition(current: SessionState, target: SessionState) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)


def sources_for(target: SessionState) -> frozenset[SessionState]:
    """States from which ``target`` may be entered (including itself)."""
    return frozenset(s for s, allowed in TRANSITIONS.items() if target in allowed) | {target}
