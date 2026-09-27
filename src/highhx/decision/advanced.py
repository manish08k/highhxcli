"""JEv / advanced decision-model reasoning — HighhX Pro only.

HighhX Free decides deterministically (:mod:`highhx.decision.deterministic`): fixed grammar
and registries, no model, no AI service. Reasoning *beyond* that — understanding open-ended
requests, choosing tools dynamically, replanning — is a Pro capability, granted by the HighhX
platform and never by anything local (a cached account grants nothing).

What provides it today: this repository has no separate JEv service, SDK or endpoint. The
Pro capability that performs advanced reasoning is the HighhX Pro agent
(:class:`~highhx.agent.session.AgentSession`, models through the HighhX platform gateway). Its
structured action plans run through the same action executor and, for desktop operations,
the same automation bridge / C# engine as Free (:meth:`ComputerSession.automation`).

This module is the one gate every entry point asks: a future JEv implementation belongs
behind :func:`require_advanced_reasoning`, never on the Free path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from highhx.cloud.capabilities import Capability
from highhx.core.errors import PlanRequiredError

if TYPE_CHECKING:
    from highhx.cloud.capabilities import Entitlements

ADVANCED_REASONING = Capability.AI_AGENT
"""The platform capability that grants JEv / advanced reasoning (today: the Pro agent)."""


def advanced_reasoning_available(entitlements: Entitlements) -> bool:
    """True only when the HighhX platform grants the Pro capability."""
    return entitlements.has(ADVANCED_REASONING)


def require_advanced_reasoning(entitlements: Entitlements) -> None:
    """Refuse on Free: JEv / advanced reasoning is HighhX Pro only."""
    if not advanced_reasoning_available(entitlements):
        raise PlanRequiredError(
            "JEv and advanced reasoning are part of HighhX Pro.",
            hint="HighhX Free runs known requests deterministically; `highhx login` to use HighhX Pro.",
        )
