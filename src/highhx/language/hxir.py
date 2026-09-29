"""HXIR — the HighhX Intermediate Representation of what a request means (version 1).

    {
      "version": "1",
      "request": "find the latest pdf and open it",
      "status": "resolved",
      "goal": {"type": "multi_step", "description": "find the latest pdf and open it"},
      "clauses": [{"text": "find the latest pdf", "status": "resolved", "intent": "filesystem.find"}, …],
      "entities": [{"kind": "file", "value": "docs/a.pdf", "source": "project_index",
                    "confidence": "high", "evidence": "the newest of 3 matching files"}],
      "constraints": [{"kind": "file_type", "value": "pdf"}, {"kind": "ordering", "value": "newest"}],
      "references": [{"text": "it", "kind": "pronoun", "status": "resolved", "entity": 0}],
      "actions": [{"id": "a1", "action": "filesystem.find", "inputs": {…}, "depends_on": []},
                  {"id": "a2", "action": "filesystem.open", "inputs": {"path": "docs/a.pdf"}, "depends_on": ["a1"]}],
      "ambiguities": [],
      "question": "",
      "reason": ""
    }

HXIR is *understanding*, not a capability. It says what the request means, which real entities
it names and what is still unclear. Only a ``resolved`` HXIR has actions, and they reach the
executor only through :func:`to_steps`, which validates every action against the action
catalog (known action, valid inputs, no forbidden kind) — the executor then classifies,
approves, verifies and audits each one exactly as it does for any other request.

Statuses: ``resolved`` (every clause maps to known actions and real entities), ``ambiguous``
(several real candidates; the person chooses), ``missing_information`` (something is not
known — a referent, a file), ``unsupported`` (HighhX knows what was asked and cannot do it,
or does not know the named entity), ``invalid`` (the request contradicts itself, e.g. "open it
but don't open anything"), ``open_ended`` (needs understanding beyond the deterministic
grammar — the AI agent, HighhX Pro).

Compatibility: readers accept exactly the versions they know. Adding an optional field keeps
the version; renaming, removing or changing the meaning of a field bumps it, and the reader for
the old version is kept. Unknown fields are errors (a model cannot smuggle data through).
Typed text is never serialised (only its length), so HXIR can be logged and shown.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.core.errors import ValidationError
from highhx.utils.validation import Any_, List, Map, Obj, Prop, Str

if TYPE_CHECKING:
    from highhx.actions.catalog import Catalog
    from highhx.actions.resolver import Step

HXIR_VERSION = "1"
SUPPORTED_VERSIONS = (HXIR_VERSION,)

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
MISSING = "missing_information"
UNSUPPORTED = "unsupported"
INVALID = "invalid"
OPEN_ENDED = "open_ended"
STATUSES = (RESOLVED, AMBIGUOUS, MISSING, UNSUPPORTED, INVALID, OPEN_ENDED)
CLAUSE_STATUSES = STATUSES

CONFIDENCE = ("high", "medium", "low")
"""A decision signal, not a probability: high — exact evidence (a registry name, an existing
path, a git remote, an explicit earlier entity); medium — unique but indirect evidence (a
keyword in a file name, part of a project name); low — never acted on."""

ENTITY_KINDS = (
    "application",
    "website",
    "url",
    "repository",
    "project",
    "file",
    "folder",
    "tab",
    "query",
    "service",
    "environment",
    "workflow",
    "branch",
)
REFERENCE_KINDS = ("pronoun", "ordinal", "current", "recent", "place", "selection")
REFERENCE_STATUSES = (RESOLVED, AMBIGUOUS, MISSING)
CONSTRAINT_KINDS = (
    "file_type",
    "time",
    "ordering",
    "ordinal",
    "owner",
    "state",
    "assigned_to",
    "location",
    "recency",
    "forbid",
)
FORBIDDEN = ("open", "change", "delete")
MASKED_INPUTS = frozenset({"text", "content"})
"""Inputs that carry what the person types — serialised as their length, never their value."""

_ENTITY = Obj(
    {
        "kind": Prop(Str(choices=ENTITY_KINDS), required=True),
        "value": Prop(Str(min_length=1), required=True, description="Path, URL, application or site name."),
        "label": Prop(Str()),
        "source": Prop(Str(), description="Where it was found: registry, project_index, git_remote, conversation …"),
        "confidence": Prop(Str(choices=CONFIDENCE), required=True),
        "evidence": Prop(Str(), description="Why this entity: the deterministic fact it rests on."),
        "attributes": Prop(Map(Str())),
    }
)
SCHEMA = Obj(
    {
        "version": Prop(Str(min_length=1), required=True),
        "request": Prop(Str(), required=True),
        "status": Prop(Str(choices=STATUSES), required=True),
        "goal": Prop(
            Obj({"type": Prop(Str(min_length=1), required=True), "description": Prop(Str())}),
            required=True,
        ),
        "clauses": Prop(
            List(
                Obj(
                    {
                        "text": Prop(Str(), required=True),
                        "status": Prop(Str(choices=CLAUSE_STATUSES), required=True),
                        "intent": Prop(Str()),
                        "reason": Prop(Str()),
                    }
                )
            )
        ),
        "entities": Prop(List(_ENTITY)),
        "constraints": Prop(
            List(
                Obj(
                    {
                        "kind": Prop(Str(choices=CONSTRAINT_KINDS), required=True),
                        "value": Prop(Str(min_length=1), required=True),
                    }
                )
            )
        ),
        "references": Prop(
            List(
                Obj(
                    {
                        "text": Prop(Str(min_length=1), required=True),
                        "kind": Prop(Str(choices=REFERENCE_KINDS), required=True),
                        "status": Prop(Str(choices=REFERENCE_STATUSES), required=True),
                        "entity": Prop(Any_(), description="Index into entities when resolved."),
                    }
                )
            )
        ),
        "actions": Prop(
            List(
                Obj(
                    {
                        "id": Prop(Str(min_length=1), required=True),
                        "action": Prop(Str(min_length=3), required=True),
                        "inputs": Prop(Map(Any_()), required=True),
                        "description": Prop(Str()),
                        "target": Prop(Str()),
                        "depends_on": Prop(List(Str(min_length=1))),
                    }
                )
            )
        ),
        "ambiguities": Prop(
            List(
                Obj(
                    {
                        "text": Prop(Str(min_length=1), required=True),
                        "question": Prop(Str(min_length=1), required=True),
                        "candidates": Prop(List(_ENTITY, min_items=2), required=True),
                    }
                )
            )
        ),
        "question": Prop(Str()),
        "reason": Prop(Str()),
        "correction_of": Prop(Str(), description="The earlier request this one corrects."),
    }
)


# ---------------------------------------------------------------------- model
@dataclass(frozen=True)
class Entity:
    kind: str
    value: str
    label: str = ""
    source: str = ""
    confidence: str = "high"
    evidence: str = ""
    attributes: tuple[tuple[str, str], ...] = ()

    @property
    def shown(self) -> str:
        return self.label or self.value

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "value": self.value, "confidence": self.confidence}
        for key in ("label", "source", "evidence"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.attributes:
            out["attributes"] = dict(self.attributes)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Entity:
        return cls(
            str(data["kind"]),
            str(data["value"]),
            str(data.get("label") or ""),
            str(data.get("source") or ""),
            str(data.get("confidence") or "high"),
            str(data.get("evidence") or ""),
            tuple(sorted((str(k), str(v)) for k, v in (data.get("attributes") or {}).items())),
        )


@dataclass(frozen=True)
class Clause:
    text: str
    status: str
    intent: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {"text": self.text, "status": self.status}
        if self.intent:
            out["intent"] = self.intent
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class Reference:
    text: str
    kind: str
    status: str
    entity: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"text": self.text, "kind": self.kind, "status": self.status}
        if self.entity is not None:
            out["entity"] = self.entity
        return out


@dataclass(frozen=True)
class Ambiguity:
    text: str
    question: str
    candidates: tuple[Entity, ...]
    template: str = ""
    """The clause with ``{choice}`` where the chosen candidate goes (kept in memory, not serialised)."""

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "question": self.question, "candidates": [c.to_dict() for c in self.candidates]}


@dataclass(frozen=True)
class HXAction:
    id: str
    action: str
    inputs: dict[str, Any] = field(default_factory=dict, hash=False)
    description: str = ""
    target: str = ""
    depends_on: tuple[str, ...] = ()

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        inputs = {
            k: (f"<{len(str(v))} characters>" if redact and k in MASKED_INPUTS else v)
            for k, v in sorted(self.inputs.items())
        }
        out: dict[str, Any] = {
            "id": self.id,
            "action": self.action,
            "inputs": inputs,
            "depends_on": list(self.depends_on),
        }
        if self.description:
            out["description"] = self.description
        if self.target:
            out["target"] = self.target
        return out


@dataclass(frozen=True)
class HXIR:
    request: str
    status: str
    goal_type: str
    goal_description: str = ""
    clauses: tuple[Clause, ...] = ()
    entities: tuple[Entity, ...] = ()
    constraints: tuple[tuple[str, str], ...] = ()
    references: tuple[Reference, ...] = ()
    actions: tuple[HXAction, ...] = ()
    ambiguities: tuple[Ambiguity, ...] = ()
    question: str = ""
    reason: str = ""
    correction_of: str = ""
    version: str = HXIR_VERSION

    @property
    def resolved(self) -> bool:
        return self.status == RESOLVED

    def to_dict(self, *, redact: bool = True, scrub: Callable[[str], str] | None = None) -> dict[str, Any]:
        """``scrub``: applied to every free-text field (the secret redactor, for traces and logs)."""
        out = self._to_dict(redact=redact)
        return _scrubbed(out, scrub) if scrub is not None else out

    def _to_dict(self, *, redact: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": self.version,
            "request": self.request,
            "status": self.status,
            "goal": {"type": self.goal_type, "description": self.goal_description},
            "clauses": [c.to_dict() for c in self.clauses],
            "entities": [e.to_dict() for e in self.entities],
            "constraints": [{"kind": k, "value": v} for k, v in self.constraints],
            "references": [r.to_dict() for r in self.references],
            "actions": [a.to_dict(redact=redact) for a in self.actions],
            "ambiguities": [a.to_dict() for a in self.ambiguities],
            "question": self.question,
            "reason": self.reason,
        }
        if self.correction_of:
            out["correction_of"] = self.correction_of
        return out

    def to_json(self) -> str:
        """Deterministic serialisation: the same HXIR is always the same bytes."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ------------------------------------------------------------------ validation
def _scrubbed(data: dict[str, Any], scrub: Callable[[str], str]) -> dict[str, Any]:
    """Every string the person wrote or that was derived from it, through ``scrub``."""
    data["request"] = scrub(data["request"])
    data["goal"]["description"] = scrub(data["goal"]["description"])
    for key in ("question", "reason", "correction_of"):
        if data.get(key):
            data[key] = scrub(data[key])
    for item in [*data["clauses"], *data["references"], *data["ambiguities"]]:
        for key in ("text", "reason", "question"):
            if isinstance(item.get(key), str):
                item[key] = scrub(item[key])
    for action in data["actions"]:
        action["inputs"] = {k: scrub(v) if isinstance(v, str) else v for k, v in action["inputs"].items()}
        for key in ("description", "target"):
            if action.get(key):
                action[key] = scrub(action[key])
    return data


def _forbidden_by(action: str, catalog: Catalog) -> set[str]:
    """What an action does, in the terms of execution constraints (open / change / delete)."""
    from highhx.decision.risk import risk_class
    from highhx.safety.actions import ActionKind

    spec = catalog.get(action)
    if spec is None:
        return set()
    out: set[str] = set()
    if spec.kind in (ActionKind.NAVIGATE, ActionKind.APP_LAUNCH) or action in ("browser.search", "browser.play"):
        out.add("open")
    if risk_class(spec.risk, spec.kind) != "safe" or spec.kind == ActionKind.DELETE_FILE:
        out.add("change")
    if spec.kind == ActionKind.DELETE_FILE or action == "shell.run":
        out.add("delete")  # a shell command can delete anything
    return out


def violations(actions: list[tuple[str, str]], forbid: set[str] | frozenset[str], catalog: Catalog) -> list[str]:
    """Actions (id, catalog name) that break the request's own execution constraints."""
    problems = []
    for action_id, name in actions:
        broken = sorted(_forbidden_by(name, catalog) & set(forbid))
        if broken:
            problems.append(f"{action_id} ({name}) would {' and '.join(broken)} something, which the request forbids")
    return problems


def validate(data: Any, catalog: Catalog | None = None) -> list[str]:
    """Every problem with an HXIR document: version, schema, then the rules the schema cannot say."""
    if not isinstance(data, dict):
        return ["hxir: expected an object"]
    version = data.get("version")
    if version is not None and str(version) not in SUPPORTED_VERSIONS:
        return [
            f"hxir.version: HXIR version {version!r} is not supported (this HighhX reads {', '.join(SUPPORTED_VERSIONS)})"
        ]
    errors = SCHEMA.validate(data, "hxir")
    if errors:
        return errors
    status = data["status"]
    actions = data.get("actions") or []
    entities = data.get("entities") or []
    if status == RESOLVED and data.get("ambiguities"):
        errors.append("hxir.ambiguities: a resolved request has no open ambiguities")
    if status != RESOLVED and actions:
        errors.append(f"hxir.actions: only a resolved request has actions (status is {status})")
    if status == AMBIGUOUS and not data.get("ambiguities"):
        errors.append("hxir.ambiguities: an ambiguous request lists its candidates")
    if status in (AMBIGUOUS, MISSING) and not data.get("question"):
        errors.append(f"hxir.question: a {status} request says what to ask")
    for index, ref in enumerate(data.get("references") or []):
        target = ref.get("entity")
        if target is not None and (not isinstance(target, int) or not 0 <= target < len(entities)):
            errors.append(f"hxir.references[{index}].entity: no entity {target!r}")
        if ref["status"] == RESOLVED and target is None:
            errors.append(f"hxir.references[{index}]: a resolved reference names its entity")
    seen: set[str] = set()
    for index, action in enumerate(actions):
        here = f"hxir.actions[{index}]"
        if action["id"] in seen:
            errors.append(f"{here}.id: duplicate id {action['id']!r}")
        for dep in action.get("depends_on") or []:
            if dep not in seen:
                errors.append(f"{here}.depends_on: {dep!r} is not an earlier action")
        seen.add(action["id"])
        if catalog is not None:
            spec = catalog.get(action["action"])
            if spec is None:
                errors.append(f"{here}.action: unknown action {action['action']!r}")
                continue
            errors += [
                e.replace("inputs", f"{here}.inputs", 1) for e in spec.inputs.validate(action["inputs"], "inputs")
            ]
    if catalog is not None:
        forbid = {c["value"] for c in data.get("constraints") or [] if c["kind"] == "forbid"}
        errors += [f"hxir.actions: {v}" for v in violations([(a["id"], a["action"]) for a in actions], forbid, catalog)]
    return errors


def parse(data: Any, catalog: Catalog | None = None) -> HXIR:
    """A validated :class:`HXIR` from a document (a file, a model's answer); raises
    :class:`ValidationError` listing every problem."""
    errors = validate(data, catalog)
    if errors:
        raise ValidationError("Not valid HXIR.", details=errors)
    return HXIR(
        request=str(data["request"]),
        status=str(data["status"]),
        goal_type=str(data["goal"]["type"]),
        goal_description=str(data["goal"].get("description") or ""),
        clauses=tuple(
            Clause(str(c["text"]), str(c["status"]), str(c.get("intent") or ""), str(c.get("reason") or ""))
            for c in data.get("clauses") or []
        ),
        entities=tuple(Entity.from_dict(e) for e in data.get("entities") or []),
        constraints=tuple((str(c["kind"]), str(c["value"])) for c in data.get("constraints") or []),
        references=tuple(
            Reference(str(r["text"]), str(r["kind"]), str(r["status"]), r.get("entity"))
            for r in data.get("references") or []
        ),
        actions=tuple(
            HXAction(
                str(a["id"]),
                str(a["action"]),
                dict(a["inputs"]),
                str(a.get("description") or ""),
                str(a.get("target") or ""),
                tuple(str(d) for d in a.get("depends_on") or []),
            )
            for a in data.get("actions") or []
        ),
        ambiguities=tuple(
            Ambiguity(str(a["text"]), str(a["question"]), tuple(Entity.from_dict(c) for c in a["candidates"]))
            for a in data.get("ambiguities") or []
        ),
        question=str(data.get("question") or ""),
        reason=str(data.get("reason") or ""),
        correction_of=str(data.get("correction_of") or ""),
        version=str(data["version"]),
    )


def to_steps(hxir: HXIR, catalog: Catalog) -> list[Step]:
    """The executable steps of a resolved HXIR — the only way HXIR reaches the executor.
    Re-validated against the catalog here, whoever built it."""
    from highhx.actions.resolver import Step

    if not hxir.resolved:
        raise ValidationError(f"Only a resolved request can run (this one is {hxir.status}).")
    errors = validate(hxir.to_dict(redact=False), catalog)
    if errors:
        raise ValidationError("Not valid HXIR.", details=errors)
    return [Step(a.action, dict(a.inputs), a.description, a.target) for a in hxir.actions]
