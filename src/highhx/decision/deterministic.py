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
    from datetime import datetime

    from highhx.actions.catalog import Catalog
    from highhx.actions.resolver import Resolution
    from highhx.cloud.capabilities import Capability
    from highhx.language.hxir import HXIR, Entity
    from highhx.language.references import ConversationMemory

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
    hxir: HXIR | None = None
    """What the request means, as data (:mod:`highhx.language.hxir`): entities, references,
    constraints, ambiguities — also when nothing can run."""

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
            "hxir": self.hxir.to_dict() if self.hxir else None,
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
        memory: ConversationMemory | None = None,
        now: datetime | None = None,
    ) -> None:
        from highhx.actions.catalog import default_catalog

        self.context = context or ResolverContext()
        self.catalog = catalog or default_catalog()
        self.risk_of = risk_of or catalog_risk(self.catalog)
        self.memory = memory
        """This conversation (the interactive session): what "it" and "the second one" refer to."""
        self.now = now

    def decide(self, request: str) -> Decision:
        from highhx.language.understand import from_resolution, understand

        normalized = normalise(request)
        clauses = tuple(split_clauses(normalized, VERB_WORDS | RULE_WORDS)) if normalized else ()
        resolution, unknown = _plan(request, self.context) if normalized else (None, None)
        if resolution is not None:  # the grammar alone: exactly as it always resolved
            return self._local(
                request, normalized, clauses, resolution, from_resolution(normalized, list(resolution.steps))
            )
        from highhx.agent.router import required_capability
        from highhx.cloud.capabilities import Capability
        from highhx.language.hxir import OPEN_ENDED, RESOLVED

        understood = (
            understand(request, self.context, self.memory, catalog=self.catalog, now=self.now) if normalized else None
        )
        hxir = understood.hxir if understood is not None else None
        if understood is not None and hxir is not None and hxir.status == RESOLVED:
            from highhx.actions.resolver import Resolution
            from highhx.language.hxir import to_steps

            steps = tuple(to_steps(hxir, self.catalog))  # validated against the catalog again
            resolved = Resolution(steps, hxir.request, "understand")
            parts = tuple(c.text for c in hxir.clauses)
            return self._local(request, normalized, parts, resolved, hxir)
        if understood is not None and hxir is not None and hxir.status != OPEN_ENDED:
            unknown = understood.unknown or as_unknown(hxir)
        if unknown is not None:
            return Decision(
                request,
                normalized,
                UNKNOWN,
                clauses,
                unknown=unknown,
                capability=Capability.AI_AGENT,
                reason=unknown.reason,
                hxir=hxir,
            )
        capability, reason = required_capability(normalized or request)
        if len(clauses) > MAX_CLAUSES:
            reason = f"More than {MAX_CLAUSES} steps in one request. {reason}"
        return Decision(request, normalized, PRO, clauses, capability=capability, reason=reason, hxir=hxir)

    def _local(
        self, request: str, normalized: str, clauses: tuple[str, ...], resolution: Resolution, hxir: HXIR
    ) -> Decision:
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
            hxir=hxir,
        )


def as_unknown(hxir: HXIR) -> Unknown:
    """An HXIR that cannot run, as the explanation every caller already shows (nothing runs)."""
    clause = next((c.text for c in hxir.clauses if c.status != "resolved"), hxir.request)
    if hxir.ambiguities:
        candidates = hxir.ambiguities[0].candidates
        numbered = tuple(f"{i}. {c.shown}{_detail(c)}" for i, c in enumerate(candidates, 1))
        return Unknown(clause, f"{hxir.reason} {hxir.question}".strip(), numbered)
    return Unknown(clause, hxir.question or hxir.reason or "HighhX could not resolve this request.", ())


def _detail(candidate: Entity) -> str:
    """What helps the person choose: when a file changed, or why a weaker match was offered."""
    modified = dict(candidate.attributes).get("modified")
    if modified:
        return f"  (modified {modified.replace('T', ' ')[:16]})"
    return f"  ({candidate.evidence})" if candidate.evidence and candidate.confidence != "high" else ""
