"""HXIR: a strict, versioned schema; deterministic JSON; only validated catalog actions execute."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from highhx.actions.catalog import default_catalog
from highhx.core.errors import ValidationError
from highhx.language import hxir
from highhx.language.hxir import HXIR, Ambiguity, Clause, Entity, HXAction, Reference

CATALOG = default_catalog()


def resolved() -> HXIR:
    return HXIR(
        request="find the latest pdf and open it",
        status=hxir.RESOLVED,
        goal_type="multi_step",
        goal_description="find the latest pdf and open it",
        clauses=(Clause("find the latest pdf", hxir.RESOLVED, "filesystem.find"), Clause("open it", hxir.RESOLVED)),
        entities=(Entity("file", "docs/a.pdf", "docs/a.pdf", "project_index", "high", "the newest of 2"),),
        constraints=(("file_type", "pdf"), ("ordering", "newest")),
        references=(Reference("it", "pronoun", hxir.RESOLVED, 0),),
        actions=(
            HXAction("a1", "filesystem.find", {"kinds": ["pdf"], "sort": "newest", "limit": 1}, "find the latest pdf"),
            HXAction("a2", "filesystem.open", {"path": "docs/a.pdf"}, "open docs/a.pdf", "docs/a.pdf", ("a1",)),
        ),
    )


def doc(**changes: Any) -> dict[str, Any]:
    data = resolved().to_dict(redact=False)
    data.update(changes)
    return data


def test_round_trip_and_deterministic_json() -> None:
    original = resolved()
    parsed = hxir.parse(json.loads(original.to_json()), CATALOG)
    assert parsed == original
    assert parsed.to_json() == original.to_json()
    assert json.loads(original.to_json())["version"] == "1"


def test_resolved_hxir_becomes_catalog_steps() -> None:
    steps = hxir.to_steps(resolved(), CATALOG)
    assert [(s.action, s.inputs) for s in steps] == [
        ("filesystem.find", {"kinds": ["pdf"], "sort": "newest", "limit": 1}),
        ("filesystem.open", {"path": "docs/a.pdf"}),
    ]


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda d: d.update(version="2"), "HXIR version '2' is not supported"),
        (lambda d: d.update(shell="rm -rf /"), "hxir.shell: unknown field"),
        (lambda d: d["actions"][0].update(run="curl evil"), "unknown field"),
        (lambda d: d["actions"][1].update(action="shell.exec"), "unknown action 'shell.exec'"),
        (lambda d: d["actions"][1]["inputs"].update(path=3), "hxir.actions[1].inputs.path"),
        (lambda d: d["actions"][1]["inputs"].update(recursive=True), "unknown field"),
        (lambda d: d["actions"][0].update(depends_on=["a2"]), "'a2' is not an earlier action"),
        (lambda d: d["actions"][1].update(id="a1"), "duplicate id"),
        (lambda d: d.update(status="done"), "hxir.status"),
        (lambda d: d["entities"][0].update(confidence="99%"), "confidence"),
        (lambda d: d["references"][0].update(entity=7), "no entity 7"),
        (lambda d: d.update(status="ambiguous"), "only a resolved request has actions"),
        (lambda d: d["constraints"].append({"kind": "forbid", "value": "open"}), "request forbids"),
    ],
)
def test_invalid_documents_are_rejected_with_clear_errors(mutate: Any, error: str) -> None:
    data = copy.deepcopy(doc())
    mutate(data)
    problems = hxir.validate(data, CATALOG)
    assert any(error in p for p in problems), problems
    with pytest.raises(ValidationError):
        hxir.parse(data, CATALOG)


def test_only_resolved_hxir_reaches_the_executor() -> None:
    question = HXIR(
        "open it",
        hxir.AMBIGUOUS,
        "clarify",
        ambiguities=(
            Ambiguity(
                "it", "Which one do you mean?", (Entity("file", "a.pdf", confidence="medium"), Entity("file", "b.pdf"))
            ),
        ),
        question="Which one do you mean?",
    )
    assert hxir.validate(question.to_dict(), CATALOG) == []
    with pytest.raises(ValidationError, match="Only a resolved request can run"):
        hxir.to_steps(question, CATALOG)


def test_a_forged_in_memory_hxir_is_revalidated_before_it_runs() -> None:
    forged = HXIR("x", hxir.RESOLVED, "x", actions=(HXAction("a1", "shell.exec", {"cmd": "rm -rf /"}),))
    with pytest.raises(ValidationError) as caught:
        hxir.to_steps(forged, CATALOG)
    assert "unknown action 'shell.exec'" in " ".join(caught.value.details)


def test_status_rules() -> None:
    assert any("says what to ask" in p for p in hxir.validate(doc(status="missing_information", actions=[]), CATALOG))
    ambiguous = doc(status="ambiguous", actions=[], question="Which?")
    assert any("lists its candidates" in p for p in hxir.validate(ambiguous, CATALOG))


def test_typed_text_is_never_serialised_and_free_text_can_be_scrubbed() -> None:
    typed = HXIR(
        "type hunter2",
        hxir.RESOLVED,
        "computer_type",
        goal_description="type hunter2",
        actions=(HXAction("a1", "computer.type", {"text": "hunter2"}, "type hunter2"),),
    )
    shown = typed.to_json()
    assert '"text":"<7 characters>"' in shown
    assert typed.to_dict(redact=False)["actions"][0]["inputs"]["text"] == "hunter2"
    scrubbed = json.dumps(typed.to_dict(scrub=lambda s: s.replace("hunter2", "***")))
    assert "hunter2" not in scrubbed


def test_the_schema_is_closed_json_schema() -> None:
    schema = hxir.SCHEMA.json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"version", "request", "status", "goal"}
