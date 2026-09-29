"""References resolve from facts only — one clear referent, or a question, never a guess."""

from __future__ import annotations

import pytest

from highhx.actions.resolver import Step
from highhx.language.hxir import AMBIGUOUS, MISSING, RESOLVED, Entity
from highhx.language.references import ConversationMemory, detect, entities_of_step, resolve, split_reference

PROJECT = Entity("project", ".", "app", "project")
GITHUB = Entity("website", "https://github.com", "GitHub", "conversation")
REPO = Entity("url", "https://github.com/me/app", "https://github.com/me/app", "conversation")
README = Entity("file", "README.md", "README.md", "conversation")
LOGO = Entity("file", "assets/logo.png", "assets/logo.png", "conversation")


def memory(*turns: list[Entity], results: list[Entity] | None = None) -> ConversationMemory:
    mem = ConversationMemory()
    for index, entities in enumerate(turns):
        mem.record(f"request {index}", entities, results if index == len(turns) - 1 else None)
    return mem


@pytest.mark.parametrize(
    ("text", "kind", "noun", "ordinal"),
    [
        ("it", "pronoun", "", None),
        ("that one", "pronoun", "", None),
        ("the second one", "ordinal", "one", 2),
        ("the 2nd file", "ordinal", "file", 2),
        ("number 3", "ordinal", "", 3),
        ("the last one", "ordinal", "one", -1),
        ("the file I just opened", "recent", "file", None),
        ("the file we were working on", "recent", "file", None),
        ("the image I opened earlier", "recent", "image", None),
        ("the previous tab", "recent", "tab", None),
        ("that tab", "recent", "tab", None),
        ("the current page", "current", "page", None),
        ("the current repository", "current", "project", None),
        ("the project I'm working on", "current", "project", None),
        ("here", "place", "here", None),
        ("there", "place", "there", None),
    ],
)
def test_detection(text: str, kind: str, noun: str, ordinal: int | None) -> None:
    ref = detect(text)
    assert ref is not None and (ref.kind, ref.noun, ref.ordinal) == (kind, noun, ordinal)


@pytest.mark.parametrize("text", ["github", "my repo", "the readme", "python tutorials", "it rains", "the admin page"])
def test_names_and_descriptions_are_not_references(text: str) -> None:
    assert detect(text) is None


def test_split_keeps_the_verb_and_the_tail() -> None:
    head, ref, tail = split_reference("open it in chrome")  # type: ignore[misc]
    assert (head, ref.kind, tail) == ("open", "pronoun", " in chrome")
    assert split_reference("search for rock and roll") is None


def test_a_pronoun_means_the_single_entity_of_the_last_turn() -> None:
    result = resolve(detect("it"), memory([GITHUB], [README]), [], PROJECT)  # type: ignore[arg-type]
    assert result.status == RESOLVED and result.entity == README


def test_a_pronoun_with_several_candidates_is_a_question() -> None:
    result = resolve(detect("it"), memory([GITHUB, REPO]), [], PROJECT)  # type: ignore[arg-type]
    assert result.status == AMBIGUOUS and result.candidates == (GITHUB, REPO)


def test_a_pronoun_never_reaches_past_the_last_turn() -> None:
    mem = memory([README], [])  # the last turn acted on nothing nameable (e.g. a web search)
    result = resolve(detect("it"), mem, [], PROJECT)  # type: ignore[arg-type]
    assert result.status == MISSING and "last step" in result.question


def test_nothing_to_refer_to() -> None:
    assert resolve(detect("that"), None, [], PROJECT).status == MISSING  # type: ignore[arg-type]
    assert resolve(detect("that"), ConversationMemory(), [], PROJECT).status == MISSING  # type: ignore[arg-type]


def test_earlier_clauses_of_the_same_request_come_first() -> None:
    result = resolve(detect("it"), memory([GITHUB]), [[README]], PROJECT)  # type: ignore[arg-type]
    assert result.entity == README


def test_descriptions_search_back_for_their_kind() -> None:
    mem = memory([README], [GITHUB], [LOGO])
    assert resolve(detect("the file I just opened"), mem, [], PROJECT).entity == LOGO  # type: ignore[arg-type]
    assert resolve(detect("the image I opened earlier"), mem, [], PROJECT).entity == LOGO  # type: ignore[arg-type]
    assert resolve(detect("the page we were working on"), mem, [], PROJECT).entity == GITHUB  # type: ignore[arg-type]


def test_the_previous_one_is_before_the_most_recent() -> None:
    mem = memory([GITHUB], [REPO])
    assert resolve(detect("the previous tab"), mem, [], PROJECT).entity == GITHUB  # type: ignore[arg-type]
    only = resolve(detect("the previous tab"), memory([GITHUB]), [], PROJECT)  # type: ignore[arg-type]
    assert only.status == MISSING


def test_ordinals_pick_from_the_last_list() -> None:
    mem = memory([README, LOGO], results=[README, LOGO])
    assert resolve(detect("the second one"), mem, [], PROJECT).entity == LOGO  # type: ignore[arg-type]
    assert resolve(detect("the last one"), mem, [], PROJECT).entity == LOGO  # type: ignore[arg-type]
    beyond = resolve(detect("the third one"), mem, [], PROJECT)  # type: ignore[arg-type]
    assert beyond.status == MISSING and "only 2" in beyond.question
    assert resolve(detect("the first one"), ConversationMemory(), [], PROJECT).status == MISSING  # type: ignore[arg-type]


def test_here_and_the_current_project() -> None:
    assert resolve(detect("here"), None, [], PROJECT).entity == PROJECT  # type: ignore[arg-type]
    assert resolve(detect("the current repository"), None, [], PROJECT).entity == PROJECT  # type: ignore[arg-type]
    assert resolve(detect("here"), None, [], None).status == MISSING  # type: ignore[arg-type]


def test_memory_is_bounded_and_clearable() -> None:
    mem = ConversationMemory()
    for index in range(50):
        mem.record(str(index), [Entity("file", f"f{index}")])
    assert len(mem.turns) == 20
    mem.ask("open the pdf", "open {choice}", (README, LOGO))
    assert mem.pending is not None and mem.results == (README, LOGO)
    mem.clear()
    assert not mem.turns and mem.pending is None and mem.results == ()


def test_entities_come_from_what_steps_did() -> None:
    assert entities_of_step(Step("browser.open", {"url": "https://github.com"}))[0][0].kind == "website"
    assert entities_of_step(Step("browser.open", {"url": "https://github.com/me/app"}))[0][0].kind == "url"
    assert entities_of_step(Step("computer.launch", {"name": "Slack"}))[0] == [
        Entity("application", "Slack", "Slack", "conversation")
    ]
    assert entities_of_step(Step("filesystem.open", {"path": "."}))[0][0].kind == "project"
    typed, _ = entities_of_step(Step("computer.type", {"text": "secret"}))
    assert typed == []  # typed text never becomes context
    found, results = entities_of_step(Step("filesystem.find", {}), {"files": [{"path": "a.pdf"}, {"path": "b.pdf"}]})
    assert [e.value for e in found] == ["a.pdf", "b.pdf"] and results == found
    _, listed = entities_of_step(
        Step("filesystem.list", {"path": "docs"}), {"path": "docs", "entries": [{"name": "img/"}, {"name": "a.md"}]}
    )
    assert [(e.kind, e.value) for e in listed or []] == [("folder", "docs/img"), ("file", "docs/a.md")]


def test_there_is_the_most_recent_place() -> None:
    mem = memory([README], [GITHUB, REPO])
    assert resolve(detect("there"), mem, [], PROJECT).entity == REPO  # type: ignore[arg-type]
