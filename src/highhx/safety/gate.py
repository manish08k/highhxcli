"""The action gate: the single, deterministic checkpoint every automated action passes.

    describe → classify (SafetyPolicy) → project policy (policies.yaml)
             → approval (mode, grants, human confirmation → ticket)
             → redeem ticket against the action as executed → execute → audit

Used by deterministic automation (`highhx computer`, `highhx do`) and by the
agent alike. The AI never reaches execution except through this gate.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from highhx.approvals.risk import RiskLevel
from highhx.core.errors import (
    ApprovalDeniedError,
    HighhXError,
    OperationCancelledError,
    PolicyViolationError,
    TimeoutExpiredError,
)
from highhx.safety.actions import ActionDescriptor, Actor
from highhx.safety.audit import AuditEvent, AuditLog
from highhx.safety.classifier import SafetyPolicy, SafetyVerdict
from highhx.safety.confirmation import ApprovalTicket, ConfirmationBroker, ConfirmPrompter

if TYPE_CHECKING:
    from highhx.core.engine import Engine

log = logging.getLogger(__name__)


class ApprovalMode(StrEnum):
    ASK = "ask"
    """Ask before changes (the default)."""
    AUTO_EDIT = "auto-edit"
    """Normal-risk, non-sensitive changes run without asking; sensitive actions still ask."""
    READ_ONLY = "read-only"
    """Only read; every change is refused."""


class GatePrompter(ConfirmPrompter, Protocol):
    def ask_permission(self, action: str, details: Sequence[str], *, allow_always: bool = True) -> str:
        """For normal-risk changes: ``yes``, ``no`` or ``always``."""
        ...


@dataclass
class Authorization:
    action: ActionDescriptor
    verdict: SafetyVerdict
    decision: str
    ticket: ApprovalTicket | None
    event: AuditEvent


class ActionGate:
    def __init__(
        self,
        engine: Engine,
        prompter: GatePrompter,
        *,
        source: str,
        mode: ApprovalMode = ApprovalMode.ASK,
        assume_yes: bool = False,
        audit: AuditLog | None = None,
        policy: SafetyPolicy | None = None,
        broker: ConfirmationBroker | None = None,
        session_id: str | None = None,
        account_id: str | None = None,
    ) -> None:
        self.engine = engine
        self.prompter = prompter
        self.source = source
        self.mode = mode
        self.assume_yes = assume_yes
        self.audit = audit
        self.policy = policy or SafetyPolicy()
        self.broker = broker or ConfirmationBroker(prompter)
        self.session_id = session_id
        self.account_id = account_id
        self.grants: set[str] = set()

    # ------------------------------------------------------------------ audit
    def _record(self, event: AuditEvent) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(event)
        except Exception as exc:  # auditing must not crash automation, but must be visible in logs
            log.warning("could not write audit event: %s", exc)

    def _event(self, action: ActionDescriptor, verdict: SafetyVerdict, decision: str) -> AuditEvent:
        return AuditEvent(
            self.source,
            action,
            decision,
            verdict=verdict,
            session_id=self.session_id,
            account_id=self.account_id,
        )

    def classify(self, action: ActionDescriptor) -> SafetyVerdict:
        return self.policy.classify(action)

    # --------------------------------------------------------------- authorize
    def authorize(
        self,
        action: ActionDescriptor,
        *,
        policy_action: str,
        grant: str | None = None,
        details: Sequence[str] = (),
        engine_prompts: bool = False,
        always_confirm: bool = False,
        min_risk: RiskLevel | None = None,
        min_risk_reason: str = "",
        risk_label: str | None = None,
    ) -> Authorization:
        """Decide whether ``action`` may run. Raises (and audits) when it may not.

        ``engine_prompts``: the action runs through ``Engine.run``, which asks for anything above
        the project's ``auto_approve`` level itself when no ticket was issued here.
        ``always_confirm``: ask explicitly even for low-risk actions (deployments).
        """
        verdict = self.policy.classify(action)
        if min_risk is not None and verdict.risk < min_risk:
            # The caller knows more than the wording of the action (e.g. a database migration).
            verdict.risk = min_risk
            if min_risk_reason and min_risk_reason not in verdict.reasons:
                verdict.reasons.append(min_risk_reason)
        elif min_risk_reason and not verdict.reasons:
            verdict.reasons.append(min_risk_reason)  # a person is never asked without a reason
        verdict.risk_label = risk_label
        try:
            if self.mode == ApprovalMode.READ_ONLY and verdict.risk > RiskLevel.SAFE:
                raise ApprovalDeniedError(f"Not allowed in read-only mode: {action.summary}")
            self.engine.evaluate_policy(policy_action, command=action.command, target=action.target or None)
            if verdict.risk <= RiskLevel.SAFE and not always_confirm:
                return Authorization(action, verdict, "allowed", None, self._event(action, verdict, "allowed"))
            if verdict.blocked or verdict.requires_confirmation or always_confirm:
                ticket = self.broker.request(action, verdict, details=details, assume_yes=self.assume_yes)
                return Authorization(action, verdict, "confirmed", ticket, self._event(action, verdict, "confirmed"))
            # Normal-risk, non-sensitive change.
            auto_level = self.engine.approvals.policy.auto_approve
            if engine_prompts and verdict.risk > auto_level:
                return Authorization(action, verdict, "allowed", None, self._event(action, verdict, "allowed"))
            if self.mode == ApprovalMode.AUTO_EDIT or (grant is not None and grant in self.grants):
                return Authorization(action, verdict, "allowed", None, self._event(action, verdict, "allowed"))
            if action.actor == Actor.USER:
                # The person asked for exactly this deterministic, non-sensitive action.
                return Authorization(action, verdict, "allowed", None, self._event(action, verdict, "allowed"))
            if not self.prompter.interactive:
                if self.assume_yes:
                    return Authorization(action, verdict, "allowed", None, self._event(action, verdict, "allowed"))
                raise ApprovalDeniedError(
                    f"Not approved: {action.summary} (no interactive terminal)",
                    hint="Run in a terminal, or pass --yes / --mode auto-edit for unattended runs.",
                )
            answer = self.prompter.ask_permission(action.summary, details)
            if answer == "always" and grant is not None:
                self.grants.add(grant)
            elif answer != "yes":
                raise ApprovalDeniedError(f"Declined: {action.summary}")
            ticket = self.broker.issue(action, verdict.risk)
            return Authorization(action, verdict, "confirmed", ticket, self._event(action, verdict, "confirmed"))
        except PolicyViolationError as exc:
            event = self._event(action, verdict, "blocked" if verdict.blocked or verdict.agent_blocked else "policy")
            event.status, event.error = "skipped", exc.message
            self._record(event)
            raise
        except ApprovalDeniedError as exc:
            event = self._event(action, verdict, "denied")
            event.status, event.error = "skipped", exc.message
            self._record(event)
            raise

    # ----------------------------------------------------------------- execute
    @contextlib.contextmanager
    def executing(self, authorization: Authorization, current: ActionDescriptor | None = None) -> Iterator[AuditEvent]:
        """Redeem the ticket against the action as it is about to run, then execute and audit.

        Inside the block, engine approvals up to the confirmed risk are satisfied by the ticket
        (the person already approved this exact action), except non-bypassable rules.
        """
        event = authorization.event
        action = current or authorization.action
        if authorization.ticket is not None:
            try:
                self.broker.redeem(authorization.ticket, action)
            except ApprovalDeniedError as exc:
                event.decision, event.status, event.error = "denied", "skipped", exc.message
                self._record(event)
                raise
            event.ticket_id = authorization.ticket.ticket_id
        scope = (
            self.engine.approvals.preapproved(authorization.verdict.risk)
            if authorization.ticket is not None
            else contextlib.nullcontext()
        )
        try:
            with scope:
                yield event
            if event.status == "pending":
                event.status = "ok"
        except (KeyboardInterrupt, OperationCancelledError):
            event.status = "cancelled"
            raise
        except TimeoutExpiredError as exc:
            event.status, event.error = "timeout", exc.message
            raise
        except HighhXError as exc:
            # "unknown": the action was sent but its result was lost (it may have happened).
            event.status = "unknown" if getattr(exc, "outcome_unknown", False) else "failed"
            event.error = exc.message
            raise
        except BaseException as exc:
            event.status, event.error = "error", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._record(event)
