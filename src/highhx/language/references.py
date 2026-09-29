"""References: "it", "that tab", "the second one", "the file we were working on", "here".

    detect("it")                          → Ref(kind="pronoun")
    detect("the second one")              → Ref(kind="ordinal", ordinal=2)
    detect("the file I just opened")      → Ref(kind="recent", noun="file")
    detect("the current repository")      → Ref(kind="current", noun="project")
    resolve(ref, memory, earlier)         → Resolved / Ambiguous / Missing — never a guess

What a reference can point at comes only from facts: entities earlier clauses of the same
request produced, and entities earlier turns of this conversation actually acted on
(:class:`ConversationMemory`, recorded after execution). The rules:

* a pronoun ("it", "that", "this one") means the entities of the *immediately preceding*
  clause or turn — one entity resolves it; several are an ambiguity; none is missing
  information. It never reaches further back, so it cannot silently pick something old;
* a descriptive reference ("the file I just opened", "the previous page") searches back
  through the conversation for the most recent entity of that kind;
* an ordinal ("the second one") picks from the last list HighhX showed (search results, a
  folder listing, or the candidates of a question it asked);
* "here" is the current project; "the current project/repository" too.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.language.constraints import ORDINAL, ORDINALS
from highhx.language.hxir import AMBIGUOUS, MISSING, RESOLVED, Entity

if TYPE_CHECKING:
    from highhx.actions.resolver import Step

MAX_TURNS = 20
MAX_RESULTS = 50

NOUN_KINDS: dict[str, tuple[str, ...]] = {
    "file": ("file",),
    "document": ("file",),
    "pdf": ("file",),
    "image": ("file",),
    "photo": ("file",),
    "picture": ("file",),
    "report": ("file",),
    "folder": ("folder", "project"),
    "directory": ("folder", "project"),
    "project": ("project",),
    "repo": ("project", "repository"),
    "repository": ("project", "repository"),
    "page": ("url", "website", "repository"),
    "site": ("url", "website", "repository"),
    "website": ("url", "website", "repository"),
    "tab": ("url", "website", "repository"),
    "link": ("url", "website", "repository"),
    "app": ("application",),
    "application": ("application",),
    "program": ("application",),
    "browser": ("application",),
}
"""What entity kinds a noun in a reference can mean ("that tab" is a page HighhX opened)."""
_ANY = ("one", "thing", "result", "item", "option", "match", "choice", "entry")
_NOUN = r"(?P<noun>" + "|".join(sorted([*NOUN_KINDS, *_ANY], key=len, reverse=True)) + r")s?"
_ACTED = (
    r"(?:that\s+|which\s+)?(?:i|we|you)\s+(?:just\s+|recently\s+)?"
    r"(?:opened|was\s+working\s+on|were\s+working\s+on|worked\s+on|'?(?:m|re)\s+working\s+on|am\s+working\s+on|"
    r"are\s+working\s+on|edited|changed|used|looked\s+at|viewed|created|visited|launched|found)"
    r"(?:\s+(?:earlier|before|just\s+now|a\s+moment\s+ago|last))?"
)

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pronoun", re.compile(r"(?:it|that|this|them|those|that\s+one|this\s+one)", re.I)),
    ("place", re.compile(r"(?P<place>here|there)", re.I)),
    (
        "current",
        re.compile(
            r"(?:the\s+)?(?:current|present)\s+"
            + _NOUN
            + r"|this\s+(?P<noun2>page|tab|file|folder|directory|project|repo|repository|site|website|app|application)"
            + r"|the\s+(?P<noun3>project|repo|repository|folder|directory|app|application|file)\s+"
            + r"(?:that\s+)?(?:i'?m|i\s+am|we'?re|we\s+are)\s+(?:currently\s+)?(?:working\s+on|in)",
            re.I,
        ),
    ),
    (
        "ordinal",
        re.compile(
            rf"(?:the\s+)?(?P<n>{ORDINAL})(?:\s+{_NOUN})?(?:\s+(?:in\s+the\s+list|on\s+the\s+list|from\s+the\s+list))?"
            r"|(?:number|option|no\.?|#)\s*(?P<num>\d{1,2})",
            re.I,
        ),
    ),
    (
        "recent",
        re.compile(
            r"the\s+(?:previous|same|last|earlier|other)\s+"
            + _NOUN
            + r"|the\s+"
            + _NOUN.replace("noun", "noun2")
            + r"\s+"
            + _ACTED
            + r"|(?:that|this)\s+"
            + _NOUN.replace("noun", "noun3"),
            re.I,
        ),
    ),
)
_TAIL = re.compile(r"^(?P<ref>.+?)(?P<tail>\s+(?:in|with|using)\s+(?:the\s+)?\S.*)?$", re.I)


@dataclass(frozen=True)
class Ref:
    text: str
    kind: str
    """pronoun, place, current, ordinal or recent."""
    noun: str = ""
    ordinal: int | None = None

    @property
    def kinds(self) -> tuple[str, ...]:
        """The entity kinds this reference can mean (empty: any)."""
        if self.kind == "place":
            return (
                ("project",) if self.noun == "here" else ("url", "website", "repository", "folder", "file", "project")
            )
        return NOUN_KINDS.get(self.noun, ())

    @property
    def file_kind(self) -> str | None:
        """ "the image …": only files of that kind."""
        return {"image": "image", "photo": "image", "picture": "image", "pdf": "pdf", "document": "document"}.get(
            self.noun
        )


def detect(text: str) -> Ref | None:
    """``text`` (the object of a clause) as a reference, or None when it names something itself."""
    phrase = " ".join(text.strip().split()).rstrip(".!?")
    for kind, pattern in PATTERNS:
        m = pattern.fullmatch(phrase)
        if m is None:
            continue
        groups = {k: v for k, v in m.groupdict().items() if v}
        noun = next((groups[k].lower() for k in ("noun", "noun2", "noun3") if k in groups), "")
        noun = noun[:-1] if noun.endswith("s") and noun[:-1] in NOUN_KINDS else noun
        if kind == "place":
            return Ref(phrase, "place", groups["place"].lower())
        if kind == "ordinal":
            raw = groups.get("n") or groups.get("num") or ""
            number = ORDINALS.get(raw.lower()) or int(re.match(r"\d+", raw).group(0))  # type: ignore[union-attr]
            return Ref(phrase, "ordinal", noun, number)
        if kind == "current" and noun in ("repo", "repository"):
            noun = "project"
        return Ref(phrase, kind, noun)
    return None


def split_reference(clause: str) -> tuple[str, Ref, str] | None:
    """ "open it in chrome" → ("open", Ref(it), " in chrome"): a clause whose object is a reference."""
    words = clause.split()
    for start in range(1, len(words)):
        rest = " ".join(words[start:])
        m = _TAIL.match(rest)
        candidates = [(rest, "")]
        if m is not None and m.group("tail"):
            candidates.insert(0, (m.group("ref"), m.group("tail")))
        for obj, tail in candidates:
            ref = detect(obj)
            if ref is not None:
                return " ".join(words[:start]), ref, tail
    return None


# ------------------------------------------------------------------- memory
@dataclass(frozen=True)
class Turn:
    request: str
    entities: tuple[Entity, ...] = ()
    actions: tuple[str, ...] = ()
    """The catalog actions that ran ("browser.search"): where the person is working now."""


@dataclass(frozen=True)
class Pending:
    """A question HighhX asked ("which one do you mean?") and the clause waiting for the answer."""

    template: str
    """The clause with ``{choice}`` for the chosen candidate."""
    candidates: tuple[Entity, ...]


@dataclass
class ConversationMemory:
    """What this conversation acted on — bounded, in-process, never persisted.

    Only names, paths and addresses are kept (never typed text or file contents)."""

    turns: deque[Turn] = field(default_factory=lambda: deque(maxlen=MAX_TURNS))
    results: tuple[Entity, ...] = ()
    """The last list HighhX showed (search results, a listing): what "the second one" picks from."""
    pending: Pending | None = None
    last_request: str = ""
    """The last request as it was understood (what "no, I meant …" corrects)."""

    def record(
        self,
        request: str,
        entities: list[Entity],
        results: list[Entity] | None = None,
        actions: tuple[str, ...] = (),
    ) -> None:
        self.turns.append(Turn(request, tuple(_unique(entities)), actions))
        if results is not None:
            self.results = tuple(results[:MAX_RESULTS])
        self.pending = None
        self.last_request = request

    def ask(self, request: str, template: str, candidates: tuple[Entity, ...]) -> None:
        """HighhX asked which candidate: the answer ("the second one") completes ``template``."""
        self.pending = Pending(template, candidates)
        self.results = candidates
        self.last_request = request

    @property
    def on_a_page(self) -> bool:
        """The last turn worked in the browser (a list there is on the page, not in memory)."""
        return bool(self.turns) and any(a.startswith("browser.") for a in self.turns[-1].actions)

    def clear(self) -> None:
        self.turns.clear()
        self.results, self.pending, self.last_request = (), None, ""


def _unique(entities: list[Entity]) -> list[Entity]:
    seen: set[tuple[str, str]] = set()
    out = []
    for entity in entities:
        key = (entity.kind, entity.value)
        if key not in seen:
            seen.add(key)
            out.append(entity)
    return out


@dataclass(frozen=True)
class RefResult:
    status: str
    entity: Entity | None = None
    candidates: tuple[Entity, ...] = ()
    question: str = ""


def _fits(ref: Ref, entity: Entity) -> bool:
    if ref.kinds and entity.kind not in ref.kinds:
        return False
    wanted = ref.file_kind
    if wanted is not None:
        from highhx.project.index import kinds_of

        return wanted in kinds_of(entity.value)
    return True


def resolve(
    ref: Ref,
    memory: ConversationMemory | None,
    earlier: list[list[Entity]],
    project: Entity | None,
) -> RefResult:
    """What ``ref`` points at. ``earlier``: entities of the earlier clauses of this request, in
    order; ``project``: the current project, when there is one."""
    said = f"'{ref.text}'"
    if ref.kind == "current" and ref.noun in ("project", "folder", "directory", ""):
        if project is None:
            return RefResult(MISSING, question="HighhX is not running in a project here.")
        return RefResult(RESOLVED, project)
    if ref.kind == "place" and ref.noun == "here":
        if project is None:
            return RefResult(MISSING, question="'Here' is the current project, and HighhX is not running in one.")
        return RefResult(RESOLVED, project)
    if ref.kind == "ordinal":
        pool = memory.pending.candidates if memory and memory.pending else (memory.results if memory else ())
        pool = tuple(e for e in pool if _fits(ref, e)) if ref.noun and ref.noun not in _ANY else pool
        if not pool:
            return RefResult(MISSING, question=f"There is no list to pick {said} from — nothing was listed yet.")
        index = len(pool) - 1 if ref.ordinal == -1 else (ref.ordinal or 1) - 1
        if not 0 <= index < len(pool):
            return RefResult(MISSING, question=f"There are only {len(pool)} to choose from, so {said} does not exist.")
        return RefResult(RESOLVED, pool[index])
    # pronouns and "that tab" look only at what came immediately before; descriptions look back further
    groups: list[tuple[Entity, ...]] = [tuple(e) for e in reversed(earlier)]
    if memory is not None:
        groups += [t.entities for t in reversed(memory.turns)]
    immediate = ref.kind == "pronoun" or (ref.kind == "recent" and ref.text.lower().startswith(("that ", "this ")))
    if immediate:
        groups = groups[:1]
    if ref.kind == "recent" and re.match(r"the\s+(?:previous|other|earlier)\b", ref.text, re.I):
        # "the previous tab": the one before the most recent (the most recent is the current one)
        ordered = [e for entities in groups for e in reversed(entities) if _fits(ref, e)]
        ordered = list(dict.fromkeys(ordered))
        if len(ordered) >= 2:
            return RefResult(RESOLVED, ordered[1])
        return RefResult(MISSING, question=f"There is nothing before the current one for {said} to mean.")
    for entities in groups:
        fitting = tuple(e for e in entities if _fits(ref, e))
        if not fitting:
            continue
        if ref.kind in ("recent", "current", "place") and not immediate:
            return RefResult(RESOLVED, fitting[-1])  # "the file I just opened": the most recent one
        if len(fitting) == 1:
            return RefResult(RESOLVED, fitting[0])
        return RefResult(AMBIGUOUS, candidates=fitting, question=f"I found several things {said} could mean.")
    if immediate and (earlier or (memory is not None and memory.turns)):
        return RefResult(MISSING, question=f"The last step did not produce anything {said} could refer to.")
    return RefResult(MISSING, question=f"Nothing earlier in this conversation for {said} to refer to.")


# ------------------------------------------------------- entities from steps
def entities_of_step(step: Step, output: dict[str, Any] | None = None) -> tuple[list[Entity], list[Entity] | None]:
    """The entities a step acted on, and (for listings and searches) the list it produced."""
    from highhx.language.targets import default_registry

    inputs, action = step.inputs, step.action
    found: list[Entity] = []
    results: list[Entity] | None = None
    if action in ("browser.open", "browser.new_tab") and inputs.get("url"):
        url = str(inputs["url"])
        site = default_registry().site_for_url(url)
        is_home = site is not None and url.rstrip("/") == site.url.rstrip("/")
        kind = "website" if is_home else "url"
        found.append(Entity(kind, url, site.name if is_home and site else url, "conversation"))
    elif action in ("computer.launch", "computer.focus"):
        name = str(inputs.get("name") or inputs.get("app") or "")
        if name:
            found.append(Entity("application", name, name, "conversation"))
    elif action in ("filesystem.open", "filesystem.read", "filesystem.create", "filesystem.list") and inputs.get(
        "path", "."
    ):
        path = str(inputs.get("path") or ".")
        if path == ".":
            found.append(Entity("project", ".", "the project", "conversation"))
        else:
            kind = "folder" if inputs.get("kind") == "folder" or action == "filesystem.list" else "file"
            found.append(Entity(kind, path, path, "conversation"))
    if action == "filesystem.find" and output is not None:
        results = [Entity("file", str(f["path"]), str(f["path"]), "project_index") for f in output.get("files") or []]
        found = list(results)
    elif action == "filesystem.list" and output is not None:
        base = str(output.get("path") or inputs.get("path") or ".")
        prefix = "" if base in (".", "") else base.rstrip("/") + "/"
        results = [
            Entity(
                "folder" if str(e["name"]).endswith("/") else "file",
                prefix + str(e["name"]).rstrip("/"),
                prefix + str(e["name"]),
                "project_index",
            )
            for e in output.get("entries") or []
        ]
    return found, results
