"""Constraints in a request: what to pick, from when, in which order — and what not to do.

    "find the latest PDF I downloaded yesterday, but don't open anything"
      kinds       ("pdf",)
      time        yesterday          → a [start, end) window from the clock, see :meth:`Constraints.window`
      ordering    newest
      downloaded  True               ("I downloaded": the person's Downloads folder, not the project)
      forbid      {"open"}           (an execution constraint: no step may open anything)
      remaining   "find the"

Fixed phrase rules only — no model, no similarity. What is not recognised stays in the
remaining text (never dropped silently), so the caller can tell a topic word ("the
*architecture* document") from a constraint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "last": -1,
}
ORDINAL = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|\d{1,2}(?:st|nd|rd|th))"

FILE_NOUNS: dict[str, str | None] = {
    "pdf": "pdf",
    "image": "image",
    "photo": "image",
    "picture": "image",
    "screenshot": "image",
    "document": "document",
    "doc": "document",
    "spreadsheet": "spreadsheet",
    "presentation": "presentation",
    "slide deck": "presentation",
    "video": "video",
    "recording": None,
    "archive": "archive",
    "zip": "archive",
    "file": None,
}
"""Nouns that make a request about files, and the file kind each names (None: any kind)."""
TOPIC_NOUNS = ("report", "notes", "spec", "readme", "changelog")
"""Nouns that are both about files and a word in the file's name ("the latest report")."""

_FORBID = re.compile(
    r"(?:,?\s*(?:but|and)\s+)?(?:(?:please\s+)?(?:do\s+not|don'?t|never)\s+(?P<verb>open|launch|run|change|modify|edit|touch|delete|remove)"
    r"(?:\s+(?:anything|it|them|any\s+\w+|a\s+thing))?"
    r"|without\s+(?P<ing>opening|launching|changing|modifying|editing|touching|deleting|removing)(?:\s+(?:anything|it|them))?)",
    re.I,
)
_FORBID_KIND = {
    "open": "open",
    "launch": "open",
    "run": "change",
    "change": "change",
    "modify": "change",
    "edit": "change",
    "touch": "change",
    "delete": "delete",
    "remove": "delete",
}
_GERUND = {
    "opening": "open",
    "launching": "launch",
    "changing": "change",
    "modifying": "modify",
    "editing": "edit",
    "touching": "touch",
    "deleting": "delete",
    "removing": "remove",
}
_READ_ONLY = re.compile(r"\b(?:read[- ]only|only\s+show(?:\s+me)?|just\s+show(?:\s+me)?)\b", re.I)
_TIME = re.compile(
    r"\b(?:from\s+|on\s+|since\s+)?(?P<t>today|yesterday|this\s+week|last\s+week|this\s+month|last\s+month|recently)\b",
    re.I,
)
_ORDERING = re.compile(
    r"\b(?P<o>latest|newest|most\s+recent(?:ly\s+(?:modified|changed|edited))?|last\s+modified|oldest|earliest)\b", re.I
)
_ORDINAL = re.compile(rf"\b(?P<n>{ORDINAL})\b", re.I)
_ASSIGNED = re.compile(r"\bassigned\s+to\s+me\b", re.I)
_STATE = re.compile(r"\b(?P<s>open|closed)\s+(?=(?:issues?|pull\s+requests?|prs?|tickets?|bugs?)\b)", re.I)
_DOWNLOADED = re.compile(
    r"\b(?:(?:that\s+|which\s+)?(?:i|we)\s+(?:just\s+)?downloaded|downloaded|(?:from|in)\s+(?:my\s+|the\s+)?downloads(?:\s+folder)?)\b",
    re.I,
)
_WORKING_ON = re.compile(
    r"\b(?:that\s+)?(?:i|we)\s+(?:was|were|am|are|have\s+been|'ve\s+been)\s+working\s+on\b"
    r"|\b(?:that\s+)?(?:i|we)\s+(?:just\s+|recently\s+)?(?:edited|changed|modified|updated)\b",
    re.I,
)
_MINE = re.compile(r"\b(?:my|mine|our)\b", re.I)
_NOUN = re.compile(r"\b(?P<noun>" + "|".join(sorted(FILE_NOUNS, key=len, reverse=True)) + r")s?\b", re.I)


@dataclass(frozen=True)
class Constraints:
    kinds: tuple[str, ...] = ()
    file_noun: bool = False
    """A file noun ("file", "PDF", "document" …) was used: the request is about files."""
    time: str | None = None
    """today, yesterday, this_week, last_week, this_month, last_month or recently."""
    ordering: str | None = None
    """newest or oldest."""
    ordinal: int | None = None
    """1-based position; -1 is the last."""
    mine: bool = False
    state: str | None = None
    """open or closed (of issues, pull requests …)."""
    assigned_to_me: bool = False
    downloaded: bool = False
    """"I downloaded": the person's Downloads folder — outside any project."""
    working_on: bool = False
    """"I was working on" / "I edited": recently modified (newest first)."""
    forbid: frozenset[str] = field(default_factory=frozenset)
    """Execution constraints: open, change or delete must not happen."""

    def window(self, now: datetime) -> tuple[float, float] | None:
        """The time constraint as [start, end) epoch seconds, in the local time of ``now``."""
        if self.time is None:
            return None
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week = day - timedelta(days=day.weekday())
        month = day.replace(day=1)
        start, end = {
            "today": (day, day + timedelta(days=1)),
            "yesterday": (day - timedelta(days=1), day),
            "this_week": (week, week + timedelta(days=7)),
            "last_week": (week - timedelta(days=7), week),
            "this_month": (month, (month + timedelta(days=32)).replace(day=1)),
            "last_month": ((month - timedelta(days=1)).replace(day=1), month),
            "recently": (day - timedelta(days=7), now + timedelta(seconds=1)),
        }[self.time]
        return start.timestamp(), end.timestamp()

    @property
    def empty(self) -> bool:
        return not self.to_list()

    def to_list(self) -> list[dict[str, Any]]:
        """The constraints as HXIR items, in a fixed order."""
        items: list[dict[str, Any]] = [{"kind": "file_type", "value": k} for k in self.kinds]
        if self.time:
            items.append({"kind": "time", "value": self.time})
        if self.ordering:
            items.append({"kind": "ordering", "value": self.ordering})
        if self.ordinal is not None:
            items.append({"kind": "ordinal", "value": str(self.ordinal)})
        if self.mine:
            items.append({"kind": "owner", "value": "me"})
        if self.state:
            items.append({"kind": "state", "value": self.state})
        if self.assigned_to_me:
            items.append({"kind": "assigned_to", "value": "me"})
        if self.downloaded:
            items.append({"kind": "location", "value": "downloads"})
        if self.working_on:
            items.append({"kind": "recency", "value": "working_on"})
        items += [{"kind": "forbid", "value": f} for f in sorted(self.forbid)]
        return items


def _ordinal(word: str) -> int:
    word = word.lower()
    if word in ORDINALS:
        return ORDINALS[word]
    return int(re.match(r"\d+", word).group(0))  # type: ignore[union-attr]


def _cut(text: str, match: re.Match[str]) -> str:
    return " ".join((text[: match.start()] + " " + text[match.end() :]).split())


def extract_forbidden(text: str) -> tuple[frozenset[str], str]:
    """Only the execution constraints ("don't open anything") — they apply to the whole request."""
    forbid: set[str] = set()
    while (m := _FORBID.search(text)) is not None:
        verb = (m.group("verb") or "").lower() or _GERUND[m.group("ing").lower()]
        forbid.add(_FORBID_KIND[verb])
        text = _cut(text, m)
    if (m := _READ_ONLY.search(text)) is not None:
        forbid |= {"open", "change", "delete"}
        text = " ".join((text[: m.start()] + " show " + text[m.end() :]).split())
    return frozenset(forbid), text.strip(" ,")


def extract(text: str) -> tuple[Constraints, str]:
    """The constraints in ``text``, and the text without them."""
    forbid, text = extract_forbidden(text)
    time = ordering = state = None
    ordinal: int | None = None
    if (m := _ORDERING.search(text)) is not None:
        ordering = "oldest" if m.group("o").lower() in ("oldest", "earliest") else "newest"
        text = _cut(text, m)
    downloaded = working_on = assigned = False
    if (m := _DOWNLOADED.search(text)) is not None:
        downloaded = True
        text = _cut(text, m)
    if (m := _WORKING_ON.search(text)) is not None:
        working_on = True
        text = _cut(text, m)
    if (m := _TIME.search(text)) is not None:  # after "working on", whose "on" is not "on <day>"
        time = re.sub(r"\s+", "_", m.group("t").lower())
        text = _cut(text, m)
    if (m := _ASSIGNED.search(text)) is not None:
        assigned = True
        text = _cut(text, m)
    if (m := _STATE.search(text)) is not None:
        state = m.group("s").lower()
        text = _cut(text, m)
    if (m := _ORDINAL.search(text)) is not None:
        ordinal = _ordinal(m.group("n"))
        text = _cut(text, m)
    mine = _MINE.search(text) is not None
    kinds: list[str] = []
    file_noun = False
    for m in _NOUN.finditer(text):
        file_noun = True
        kind = FILE_NOUNS[m.group("noun").lower()]
        if kind is not None and kind not in kinds:
            kinds.append(kind)
    if not file_noun and re.search(r"\b(?:" + "|".join(TOPIC_NOUNS) + r")s?\b", text, re.I):
        file_noun = True  # "the latest report": a file whose name says "report"
    constraints = Constraints(
        kinds=tuple(kinds),
        file_noun=file_noun,
        time=time,
        ordering=ordering,
        ordinal=ordinal,
        mine=mine,
        state=state,
        assigned_to_me=assigned,
        downloaded=downloaded,
        working_on=working_on,
        forbid=forbid,
    )
    return constraints, text.strip(" ,")
