"""Normalisation keeps the meaning; constraints are extracted by fixed rules and never dropped."""

from __future__ import annotations

from datetime import datetime

import pytest

from highhx.language.constraints import Constraints, extract, extract_forbidden
from highhx.language.parser import normalise

NOW = datetime(2026, 9, 30, 15, 30)  # a Wednesday


@pytest.mark.parametrize(
    "text",
    [
        "open github",
        "open github pls",
        "open github please",
        "Open GitHub, please.",
        "can u open github",
        "could you open github?",
        "could you please open github",
        "I'd like you to open github",
        "open github thanks",
    ],
)
def test_politeness_is_trimmed_to_the_same_request(text: str) -> None:
    assert normalise(text).lower() == "open github"


def test_normalising_keeps_meaningful_words() -> None:
    assert normalise("search for me") == "search for me"  # "for me" can carry meaning: kept
    assert normalise("type 'yes please'") == "type 'yes please'"  # quoted text is never trimmed
    assert normalise("  open   the   readme  ") == "open the readme"


def test_the_spec_example_keeps_every_constraint() -> None:
    found, rest = extract("the latest PDF downloaded yesterday, but don't open anything")
    assert found.kinds == ("pdf",)
    assert found.time == "yesterday" and found.ordering == "newest"
    assert found.downloaded and found.forbid == frozenset({"open"})
    assert found.file_noun
    assert "open" not in rest and "yesterday" not in rest


@pytest.mark.parametrize(
    ("text", "forbid"),
    [
        ("show me the files without changing anything", {"change"}),
        ("list the docs, don't delete anything", {"delete"}),
        ("find it but do not open it", {"open"}),
        ("only show me the reports", {"open", "change", "delete"}),
        ("just show me the pdf", {"open", "change", "delete"}),
        ("open the readme", set()),
    ],
)
def test_execution_constraints(text: str, forbid: set[str]) -> None:
    assert extract_forbidden(text)[0] == frozenset(forbid)


@pytest.mark.parametrize(
    ("text", "field", "value"),
    [
        ("the second file", "ordinal", 2),
        ("the 3rd result", "ordinal", 3),
        ("the last one", "ordinal", -1),
        ("the oldest image", "ordering", "oldest"),
        ("the most recent report", "ordering", "newest"),
        ("files from last week", "time", "last_week"),
        ("the pdf from today", "time", "today"),
        ("open issues assigned to me", "assigned_to_me", True),
        ("closed pull requests", "state", "closed"),
        ("my documents", "mine", True),
        ("the file I was working on", "working_on", True),
    ],
)
def test_constraint_kinds(text: str, field: str, value: object) -> None:
    assert getattr(extract(text)[0], field) == value


def test_last_week_is_time_not_an_ordinal_and_latest_is_not_last() -> None:
    found, _ = extract("the latest file from last week")
    assert found.time == "last_week" and found.ordering == "newest" and found.ordinal is None


def test_a_verb_open_is_not_a_state() -> None:
    assert extract("open the issues")[0].state is None  # "open" the verb, not "open issues"


@pytest.mark.parametrize(
    ("time", "start", "end"),
    [
        ("today", datetime(2026, 9, 30), datetime(2026, 10, 1)),
        ("yesterday", datetime(2026, 9, 29), datetime(2026, 9, 30)),
        ("this_week", datetime(2026, 9, 28), datetime(2026, 10, 5)),
        ("last_week", datetime(2026, 9, 21), datetime(2026, 9, 28)),
        ("this_month", datetime(2026, 9, 1), datetime(2026, 10, 1)),
        ("last_month", datetime(2026, 8, 1), datetime(2026, 9, 1)),
    ],
)
def test_time_windows_are_calendar_ranges(time: str, start: datetime, end: datetime) -> None:
    assert Constraints(time=time).window(NOW) == (start.timestamp(), end.timestamp())


def test_file_nouns_and_topic_nouns() -> None:
    assert extract("my screenshots")[0].kinds == ("image",)
    assert extract("the architecture document")[0].kinds == ("document",)
    report = extract("the latest report")[0]
    assert report.file_noun and report.kinds == ()  # "report" is a word in the name, not a type
    assert not extract("the admin page")[0].file_noun


def test_constraints_serialise_in_a_fixed_order() -> None:
    found, _ = extract("the latest pdf from yesterday, don't open anything")
    assert found.to_list() == [
        {"kind": "file_type", "value": "pdf"},
        {"kind": "time", "value": "yesterday"},
        {"kind": "ordering", "value": "newest"},
        {"kind": "forbid", "value": "open"},
    ]
