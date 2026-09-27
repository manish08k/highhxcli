"""Human confirmation bound to one exact action.

When a person approves a sensitive action, the broker issues an
:class:`ApprovalTicket`: an HMAC (with a per-process random key) over the action's
canonical digest, a nonce and an expiry. The ticket is redeemed immediately
before execution against the action *as it is about to run*; if anything differs
— another command, another element, a relabelled button — redemption fails. A
ticket can be redeemed once. Displaying a message never counts as approval.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from highhx.approvals.risk import RiskLevel
from highhx.core.errors import ApprovalDeniedError, PolicyViolationError
from highhx.safety.actions import ActionDescriptor, Actor
from highhx.safety.classifier import SafetyVerdict

TICKET_TTL_SECONDS = 300.0


class ApprovalMismatchError(ApprovalDeniedError):
    """A ticket was presented for a different, expired or already-used action."""


@dataclass(frozen=True)
class ConfirmationRequest:
    action: str
    target: str
    application: str
    tool: str
    command: str | None
    risk: RiskLevel
    reasons: tuple[str, ...]
    irreversible: bool
    details: tuple[str, ...] = ()
    confirm_word: str | None = None
    """When set, the person must type this word (critical actions)."""
    risk_label: str | None = None
    """The risk as the caller rates it (the action catalog's five levels), when it has one."""

    @property
    def risk_name(self) -> str:
        return self.risk_label or self.risk.label


class ConfirmPrompter(Protocol):
    @property
    def interactive(self) -> bool: ...

    def confirm_action(self, request: ConfirmationRequest) -> bool:
        """Show the full request and return True only on an explicit approval."""
        ...


@dataclass(frozen=True)
class ApprovalTicket:
    ticket_id: str
    digest: str
    expires_at: float
    mac: str
    risk: RiskLevel


@dataclass
class ConfirmationBroker:
    prompter: ConfirmPrompter
    ttl: float = TICKET_TTL_SECONDS
    clock: Callable[[], float] = time.monotonic
    _key: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)
    _redeemed: set[str] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _mac(self, ticket_id: str, digest: str, expires_at: float) -> str:
        message = f"{ticket_id}|{digest}|{expires_at:.6f}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def issue(self, action: ActionDescriptor, risk: RiskLevel) -> ApprovalTicket:
        ticket_id = secrets.token_hex(8)
        expires_at = self.clock() + self.ttl
        return ApprovalTicket(
            ticket_id, action.digest, expires_at, self._mac(ticket_id, action.digest, expires_at), risk
        )

    def request(
        self,
        action: ActionDescriptor,
        verdict: SafetyVerdict,
        *,
        details: Sequence[str] = (),
        assume_yes: bool = False,
    ) -> ApprovalTicket:
        """Ask the person to approve ``action``; returns a ticket bound to it, or raises."""
        if verdict.blocked:
            raise PolicyViolationError(
                f"Blocked by HighhX safety policy: {action.summary}",
                details=verdict.reasons,
                hint="HighhX never runs this through automation. Do it yourself if you really mean it.",
            )
        if verdict.agent_blocked and action.actor == Actor.AGENT:
            raise PolicyViolationError(
                f"The AI agent may not do this: {action.summary}",
                details=verdict.reasons,
                hint="Enter credentials and payment details yourself.",
            )
        critical = verdict.risk >= RiskLevel.CRITICAL
        # `--yes` may pre-approve a user's own deterministic, non-critical actions — never the agent's.
        if assume_yes and action.actor == Actor.USER and not critical:
            return self.issue(action, verdict.risk)
        if not self.prompter.interactive:
            raise ApprovalDeniedError(
                f"Confirmation required: {action.summary}",
                details=verdict.reasons,
                hint="Sensitive actions need an interactive terminal to confirm.",
            )
        request = ConfirmationRequest(
            action=action.summary,
            target=action.target or "-",
            application=action.application or "-",
            tool=action.tool,
            command=action.command,
            risk=verdict.risk,
            reasons=tuple(verdict.reasons) or ("sensitive action",),
            irreversible=verdict.irreversible,
            details=tuple(details),
            confirm_word=("approve" if critical else None),
            risk_label=verdict.risk_label,
        )
        if not self.prompter.confirm_action(request):
            raise ApprovalDeniedError(f"Cancelled: {action.summary}", details=verdict.reasons)
        return self.issue(action, verdict.risk)

    def redeem(self, ticket: ApprovalTicket, action: ActionDescriptor) -> None:
        """Consume ``ticket`` for exactly ``action`` (as it is about to execute)."""
        expected = self._mac(ticket.ticket_id, ticket.digest, ticket.expires_at)
        if not hmac.compare_digest(expected, ticket.mac):
            raise ApprovalMismatchError("Approval ticket is not valid for this session.")
        if not hmac.compare_digest(ticket.digest, action.digest):
            raise ApprovalMismatchError(
                f"The approval was for a different action; not running: {action.summary}",
                hint="Approve the action again.",
            )
        if self.clock() > ticket.expires_at:
            raise ApprovalMismatchError("The approval expired before the action ran.", hint="Approve it again.")
        with self._lock:
            if ticket.ticket_id in self._redeemed:
                raise ApprovalMismatchError("That approval was already used.")
            self._redeemed.add(ticket.ticket_id)
