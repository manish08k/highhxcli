"""Interactive / non-interactive approval of risky actions."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from highhx.approvals.policy import ApprovalPolicy
from highhx.approvals.risk import RiskLevel
from highhx.core.errors import ApprovalDeniedError


class Prompter(Protocol):
    """UI abstraction used to ask the user."""

    @property
    def interactive(self) -> bool: ...

    def confirm(self, message: str, *, default: bool = False) -> bool: ...

    def confirm_typed(self, message: str, expected: str) -> bool: ...


@dataclass
class ApprovalDecision:
    """Outcome of an approval request."""

    approved: bool
    mode: str
    """How the decision was reached: auto, yes-flag, dry-run, interactive, non-interactive."""
    reason: str = ""


@dataclass
class ApprovalRequest:
    """Something that needs approval."""

    action: str
    risk: RiskLevel
    details: Sequence[str] = ()
    identifiers: Sequence[str] = ()
    bypassable: bool = True
    confirm_word: str = "yes"


class ApprovalManager:
    """Applies :class:`ApprovalPolicy` to requests.

    * ``risk <= auto_approve``: approved automatically.
    * ``--dry-run``: approved as nothing will actually execute.
    * ``--yes``: approves up to ``yes_max_risk`` unless the request is marked
      non-bypassable (by a rule or by policy).
    * otherwise the user is asked; without a terminal the request is denied.
    """

    def __init__(
        self,
        policy: ApprovalPolicy,
        prompter: Prompter,
        *,
        assume_yes: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.policy = policy
        self.prompter = prompter
        self.assume_yes = assume_yes
        self.dry_run = dry_run
        self._lock = threading.Lock()
        self.history: list[tuple[ApprovalRequest, ApprovalDecision]] = []

    def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """Decide without raising."""
        decision = self._decide(request)
        self.history.append((request, decision))
        return decision

    def _decide(self, request: ApprovalRequest) -> ApprovalDecision:
        non_bypassable = (not request.bypassable) or self.policy.is_non_bypassable(
            [request.action, *request.identifiers]
        )
        if request.risk <= self.policy.auto_approve and not non_bypassable:
            return ApprovalDecision(True, "auto")
        if self.dry_run:
            return ApprovalDecision(True, "dry-run", "dry run: nothing will be executed")
        if self.assume_yes and not non_bypassable and request.risk <= self.policy.yes_max_risk:
            return ApprovalDecision(True, "yes-flag")
        if not self.prompter.interactive:
            if non_bypassable:
                reason = "this action is non-bypassable and requires interactive confirmation"
            elif self.assume_yes:
                reason = f"--yes may approve at most '{self.policy.yes_max_risk.label}' risk"
            else:
                reason = "confirmation required but no interactive terminal is available (use --yes)"
            return ApprovalDecision(False, "non-interactive", reason)
        with self._lock:
            message = f"{request.action} [risk: {request.risk.label}]"
            if request.risk >= self.policy.typed_confirmation or non_bypassable:
                ok = self.prompter.confirm_typed(message, request.confirm_word)
            else:
                ok = self.prompter.confirm(f"{message} — continue?", default=False)
        return ApprovalDecision(ok, "interactive", "" if ok else "declined by user")

    def require(self, request: ApprovalRequest) -> ApprovalDecision:
        """Decide and raise :class:`ApprovalDeniedError` if not approved."""
        decision = self.decide(request)
        if not decision.approved:
            hint = None
            if decision.mode == "non-interactive" and "--yes" in decision.reason:
                hint = "Re-run in an interactive terminal, or pass --yes to approve."
            elif decision.mode == "non-interactive":
                hint = "Re-run this command in an interactive terminal."
            raise ApprovalDeniedError(
                f"Not approved: {request.action} ({decision.reason})",
                hint=hint,
                details=list(request.details),
            )
        return decision
