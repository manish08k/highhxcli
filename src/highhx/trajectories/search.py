"""Finding similar past tasks: structured and fuzzy, deterministic, no model needed.

    score = 0.45 · semantic   cosine of normalized-term vectors (stems + UI synonyms), or of an
                              injected EmbeddingModel's vectors when one is configured
          + 0.25 · fuzzy      character-trigram overlap (morphology, typos: invoice ≈ invoices)
          + 0.20 · structure  the query mentions the task's site, app, target labels or surface
          + 0.10 · outcome    completed tasks first (they are worth repeating)

What is indexed: the goal, each step's intent, action type and target label, the sites (URL hosts)
and applications seen, the surface, the status. Filters (``surface``, ``host``, ``status``) narrow
the candidates before ranking. Equal scores keep the newest first, so results are stable.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from highhx.models.interfaces import EmbeddingModel
    from highhx.trajectories.store import Trajectory

_WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset(
    (
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "do",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "then",
        "to",
        "with",
        "my",
        "me",
        "this",
        "that",
        "please",
    )
)
SYNONYMS: tuple[frozenset[str], ...] = tuple(
    frozenset(group.split())
    for group in (
        "export download save",
        "delete remove erase trash discard",
        "signin login logon authenticate",
        "signout logout logoff",
        "open navigate visit goto launch",
        "search find lookup query",
        "submit send post confirm",
        "create add new make",
        "edit change update modify",
        "buy purchase checkout pay order",
        "settings preferences options configuration",
        "invoice bill receipt",
        "email mail message",
        "close quit exit",
    )
)
_CANON = {word: min(group) for group in SYNONYMS for word in group}
WEIGHTS = {"semantic": 0.45, "fuzzy": 0.25, "structure": 0.20, "outcome": 0.10}


def stem(word: str) -> str:
    """A light stemmer: variants of a word agree (invoices/invoice, settings/setting/set,
    exporting/exported/export, companies/company, boxes/box)."""
    if len(word) > 4 and word.endswith(("sses", "xes", "zes", "ches", "shes")):
        word = word[:-2]
    elif len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    for suffix in ("ation", "ing", "ed"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            word = word[: -len(suffix)]
            if len(word) > 2 and word[-1] == word[-2] and word[-1] not in "aeiouls":
                word = word[:-1]  # sett → set, logg → log
            break
    return word


def terms(text: str) -> list[str]:
    """Normalized terms: lower case, stop words dropped, stemmed, synonyms mapped to one word,
    "sign in" / "log in" joined."""
    words = _WORD.findall(
        text.lower()
        .replace("sign in", "signin")
        .replace("log in", "login")
        .replace("sign out", "signout")
        .replace("log out", "logout")
    )
    out = []
    for word in words:
        if word in STOP:
            continue
        base = stem(word)
        out.append(_CANON.get(word, _CANON.get(base, base)))
    return out


def trigrams(text: str) -> set[str]:
    padded = f"  {' '.join(_WORD.findall(text.lower()))}  "
    return {padded[i : i + 3] for i in range(len(padded) - 2)} if padded.strip() else set()


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[t] * b[t] for t in a.keys() & b.keys())
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


@dataclass
class Indexed:
    trajectory: Trajectory
    text: str
    terms: Counter[str]
    grams: set[str]
    hosts: set[str] = field(default_factory=set)
    apps: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)
    actions: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Ranked:
    trajectory: Trajectory
    score: float
    parts: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.trajectory.id,
            "task": self.trajectory.task,
            "score": round(self.score, 3),
            "parts": {k: round(v, 3) for k, v in self.parts.items()},
        }


def index(trajectory: Trajectory) -> Indexed:
    hosts, apps, labels, actions = set(), set(), set(), set()
    pieces = [trajectory.task, trajectory.summary]
    for step in trajectory.steps:
        obs = step.observation or {}
        host = (urlparse(str(obs.get("url") or "")).hostname or "").removeprefix("www.")
        if host:
            hosts.add(host)
        if obs.get("app"):
            apps.add(str(obs["app"]).lower())
        target = step.action.get("target") or {}
        label = str(target.get("label") or "")
        if label:
            labels.add(label.lower())
        if step.action.get("action_type"):
            actions.add(str(step.action["action_type"]))
        pieces += [step.intent, label]
    text = " ".join(p for p in pieces if p)
    return Indexed(trajectory, text, Counter(terms(text)), trigrams(trajectory.task), hosts, apps, labels, actions)


class TrajectoryIndex:
    def __init__(self, trajectories: Iterable[Trajectory], *, embedding: EmbeddingModel | None = None) -> None:
        self.items = [index(t) for t in trajectories]
        self.embedding = embedding

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.12,
        surface: str | None = None,
        host: str | None = None,
        status: str | None = None,
    ) -> list[Ranked]:
        pool = [
            i
            for i in self.items
            if (surface is None or i.trajectory.surface == surface)
            and (status is None or i.trajectory.status == status)
            and (host is None or any(h == host.removeprefix("www.") or h.endswith("." + host) for h in i.hosts))
        ]
        if not pool or not query.strip():
            return []
        wanted_terms = Counter(terms(query))
        wanted_grams = trigrams(query)
        lowered = query.lower()
        semantic = self._semantic(query, wanted_terms, pool)
        ranked = []
        for item, sem in zip(pool, semantic, strict=True):
            fuzzy = (
                len(wanted_grams & item.grams) / len(wanted_grams | item.grams) if wanted_grams and item.grams else 0.0
            )
            mentions = [h for h in item.hosts if h.split(".")[0] in lowered or h in lowered]
            mentions += [a for a in item.apps if a in lowered]
            mentions += [lab for lab in item.labels if len(lab) > 2 and lab in lowered]
            structure = min(1.0, 0.5 * len(mentions)) + (
                0.25 if item.trajectory.surface and item.trajectory.surface in lowered else 0.0
            )
            outcome = 1.0 if item.trajectory.status == "completed" else 0.0
            parts = {"semantic": sem, "fuzzy": fuzzy, "structure": min(1.0, structure), "outcome": outcome}
            score = sum(WEIGHTS[k] * v for k, v in parts.items())
            if sem == 0.0 and fuzzy < 0.2 and not mentions:
                continue  # nothing in common but the outcome: not a match
            ranked.append(Ranked(item.trajectory, score, parts))
        ranked.sort(key=lambda r: (-round(r.score, 6), -r.trajectory.started))
        return [r for r in ranked if r.score >= min_score][:limit]

    def _semantic(self, query: str, wanted: Counter[str], pool: Sequence[Indexed]) -> list[float]:
        if self.embedding is not None:
            from highhx.models.adapters import cosine

            vectors = self.embedding.embed([query, *(i.text for i in pool)])
            return [max(0.0, cosine(vectors[0], v)) for v in vectors[1:]]
        return [_cosine(wanted, i.terms) for i in pool]
