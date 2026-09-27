"""HighhX Free's deterministic decision: request → decision → JSON action plan, with no model.

    request ──normalise──► clauses ──grammar + developer rules──► steps ──planner──► JSON plan
                                                         │
                                                         ├─ an entity it does not know  → route "unknown" (explained)
                                                         └─ a clause it cannot parse    → route "pro" (open-ended)

A :class:`Decision` is plain data: the route, intent, target, entities, clauses, the plan
(with each step's risk, executor and verification) or the reason it has none. It never
calls a model, never reads provider keys, and gives the same decision for the same request
and project. This is *not* JEv: JEv / advanced decision-model reasoning is HighhX Pro only
(:mod:`highhx.decision.advanced`). Execution is somebody else's job: the plan runner runs
each step through the action executor, which classifies and approves it again.

    >>> DeterministicDecider(ResolverContext()).decide("open Gmail").route
    'local'
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.actions.resolver import MAX_CLAUSES, RULE_WORDS, ResolverContext, _plan
from highhx.language.grammar import VERB_WORDS, Unknown
from highhx.language.parser import normalise, split_clauses
from highhx.plans.planner import RiskOf, build_plan, catalog_risk
from highhx.plans.schema import ActionPlan

if TYPE_CHECKING:
    from highhx.actions.catalog import Catalog
    from highhx.actions.resolver import Resolution
    from highhx.cloud.capabilities import Capability

LOCAL, UNKNOWN, PRO = "local", "unknown", "pro"


@dataclass(frozen=True)
class Decision:
    request: str
    normalized: str
    route: str
    """``local`` (Free runs the plan), ``unknown`` (recognised, but an entity is not known —
    explained, nothing runs) or ``pro`` (open-ended: needs the AI agent)."""
    clauses: tuple[str, ...] = ()
    intent: str = ""
    target: str = ""
    entities: dict[str, list[str]] = field(default_factory=dict)
    plan: ActionPlan | None = None
    resolution: Resolution | None = None
    unknown: Unknown | None = None
    capability: Capability | None = None
    reason: str = ""
    rule: str = ""

    @property
    def supported(self) -> bool:
        """Free can carry this out locally."""
        return self.route == LOCAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "normalized": self.normalized,
            "route": self.route,
            "supported": self.supported,
            "decision": "deterministic" if self.route == LOCAL else self.route,
            "intent": self.intent,
            "target": self.target,
            "entities": self.entities,
            "clauses": list(self.clauses),
            "rule": self.rule,
            "risk": self.plan.risk if self.plan else None,
            "executor": self.plan.executor if self.plan else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "unknown": (
                {
                    "clause": self.unknown.clause,
                    "reason": self.unknown.reason,
                    "suggestions": list(self.unknown.suggestions),
                }
                if self.unknown
                else None
            ),
            "capability": str(self.capability) if self.capability else None,
            "reason": self.reason,
        }


_ENTITY_KEYS = {
    "url": "urls",
    "site": "sites",
    "app": "apps",
    "name": "apps",
    "query": "queries",
    "path": "paths",
    "command": "commands",
    "keys": "keys",
    "key": "keys",
    "environment": "environments",
    "services": "services",
    "service": "services",
    "ref": "refs",
    "message": "messages",
}


def entities_of(resolution: Resolution) -> dict[str, list[str]]:
    """The entities the plan uses, by kind (typed text is counted, never copied)."""
    found: dict[str, list[str]] = {}
    for step in resolution.steps:
        for key, value in step.inputs.items():
            kind = _ENTITY_KEYS.get(key)
            if kind is None:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                text = str(item)
                if text and text not in found.setdefault(kind, []):
                    found[kind].append(text)
        if step.action in ("computer.type", "browser.fill"):
            found.setdefault("text", []).append(f"<{len(str(step.inputs.get('text', '')))} characters>")
    return found


class DeterministicDecider:
    """Decide what a plain-language request means, deterministically (HighhX Free)."""

    def __init__(
        self,
        context: ResolverContext | None = None,
        *,
        catalog: Catalog | None = None,
        risk_of: RiskOf | None = None,
    ) -> None:
        from highhx.actions.catalog import default_catalog

        self.context = context or ResolverContext()
        self.catalog = catalog or default_catalog()
        self.risk_of = risk_of or catalog_risk(self.catalog)

    def decide(self, request: str) -> Decision:
        normalized = normalise(request)
        clauses = tuple(split_clauses(normalized, VERB_WORDS | RULE_WORDS)) if normalized else ()
        resolution, unknown = _plan(request, self.context) if normalized else (None, None)
        if resolution is not None:
            plan = build_plan(normalized, list(resolution.steps), self.catalog, self.risk_of)
            return Decision(
                request,
                normalized,
                LOCAL,
                clauses,
                intent=plan.intent,
                target=plan.target,
                entities=entities_of(resolution),
                plan=plan,
                resolution=resolution,
                rule=resolution.rule,
            )
        from highhx.agent.router import required_capability
        from highhx.cloud.capabilities import Capability

        if unknown is not None:
            return Decision(
                request,
                normalized,
                UNKNOWN,
                clauses,
                unknown=unknown,
                capability=Capability.AI_AGENT,
                reason=unknown.reason,
            )
        capability, reason = required_capability(normalized or request)
        if len(clauses) > MAX_CLAUSES:
            reason = f"More than {MAX_CLAUSES} steps in one request. {reason}"
        return Decision(request, normalized, PRO, clauses, capability=capability, reason=reason)
