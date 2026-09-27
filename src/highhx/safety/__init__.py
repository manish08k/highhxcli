"""HighhX safety layer — deterministic, model-independent, shared by Free automation and the Pro agent.

* :mod:`~highhx.safety.actions`      canonical action descriptions
* :mod:`~highhx.safety.classifier`   semantic risk classification (commands, UI, files, deploys)
* :mod:`~highhx.safety.confirmation` human confirmation bound to one exact action (tickets)
* :mod:`~highhx.safety.gate`         the checkpoint every automated action passes
* :mod:`~highhx.safety.audit`        append-only, redacted audit trail
* :mod:`~highhx.safety.injection`    untrusted-content framing (prompt-injection defence)
"""

from highhx.safety.actions import ActionDescriptor, ActionKind, Actor, attrs
from highhx.safety.classifier import SafetyPolicy, SafetyVerdict
from highhx.safety.confirmation import ApprovalTicket, ConfirmationBroker, ConfirmationRequest
from highhx.safety.gate import ActionGate, ApprovalMode, Authorization

__all__ = [
    "ActionDescriptor",
    "ActionGate",
    "ActionKind",
    "Actor",
    "ApprovalMode",
    "ApprovalTicket",
    "Authorization",
    "ConfirmationBroker",
    "ConfirmationRequest",
    "SafetyPolicy",
    "SafetyVerdict",
    "attrs",
]
