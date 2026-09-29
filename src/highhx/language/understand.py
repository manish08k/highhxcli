"""Understanding a request the deterministic grammar alone does not resolve → HXIR.

    request ─ normalise ─ correction? ("no, I meant GitHub") ─ answer to a question? ("the second one")
            ─ execution constraints ("don't open anything") ─ clauses
    clause  ─ reference? ("open it", "the file we were working on", "run the tests here")
            ─ developer rules / verb grammar (unchanged)       (:mod:`highhx.actions.resolver`, :mod:`.grammar`)
            ─ repository on a site, project by name, files by kind / time / topic
    → HXIR: resolved (actions), ambiguous (candidates), missing_information, unsupported,
      invalid or open_ended (HighhX Pro)

The decider (:mod:`highhx.decision.deterministic`) calls this only when the existing resolver
does not resolve a request, so everything that resolved before resolves exactly as before.
Nothing here executes anything: the file index is read (names and dates, inside the project),
references come from what this conversation actually did, and a resolved HXIR reaches the
executor only as validated catalog actions (:func:`highhx.language.hxir.to_steps`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from highhx.language import constraints as cons
from highhx.language.hxir import (
    AMBIGUOUS,
    HXIR,
    INVALID,
    MISSING,
    OPEN_ENDED,
    RESOLVED,
    UNSUPPORTED,
    Ambiguity,
    Clause,
    Entity,
    HXAction,
    Reference,
    violations,
)
from highhx.language.parser import normalise
from highhx.language.references import ConversationMemory, Ref, detect, entities_of_step, split_reference
from highhx.language.references import resolve as resolve_reference

if TYPE_CHECKING:
    from highhx.actions.catalog import Catalog
    from highhx.actions.resolver import ResolverContext, Step
    from highhx.language.grammar import PlanState, Unknown
    from highhx.language.targets import Site
    from highhx.project.index import FileEntity

EXTRA_WORDS = frozenset({"find", "locate", "use", "pick", "choose", "select", "take", "head", "look", "compare"})
"""Words that start a clause here (besides the grammar's verbs and the developer rules')."""
MAX_CANDIDATES = 10

_VERB_HEAD = re.compile(
    r"^(?:go\s+(?:back\s+)?to|take\s+me\s+(?:back\s+)?to|head\s+(?:back\s+)?to|navigate\s+to|browse\s+to|"
    r"switch\s+to|look\s+up|listen\s+to|search(?:\s+for)?|focus(?:\s+on)?|show(?:\s+me)?|"
    r"open|launch|visit|start|find|locate|close|run|read|play|watch|google)\b",
    re.I,
)
_CORRECTION = re.compile(
    r"^(?:(?:no|nope|sorry|oops|wait|sorry,?\s+no)[,.!]*\s+)*"
    r"(?:(?:no\s+)?i\s+meant?|i\s+actually\s+meant?|actually|rather|instead)[,:]?\s+(?P<new>.+?)(?:\s+instead)?$",
    re.I,
)
_NO_THEN_REQUEST = re.compile(r"^(?:no|nope)[,.!]+\s*(?P<new>.+)$", re.I)
_ANSWER = re.compile(r"^(?:(?:use|pick|choose|select|take|go\s+with|i\s+meant?|i\s+want)\s+)?(?P<rest>.+)$", re.I)
_USE = re.compile(r"^(?:use|pick|choose|select|go\s+with)$", re.I)
_BACK = re.compile(r"^(?:go|come|head|get)\s+back(?:\s+to)?$|^return(?:\s+to)?$", re.I)
_OPENING = re.compile(r"^(?:open|view|show(?:\s+me)?|launch|go\s+to|take\s+me\s+to|visit|bring\s+up)$", re.I)
_HERE = re.compile(
    r"\s+(?:right\s+)?(?:here|in\s+here|in\s+this\s+(?:project|repo|repository|folder)|"
    r"in\s+the\s+current\s+(?:project|repo|repository|folder))$",
    re.I,
)
_FIND = re.compile(
    r"^(?:find|locate|show(?:\s+me)?|list|get(?:\s+me)?|where(?:'s|\s+is|\s+are)|search\s+for|look\s+for)\s+(?P<desc>.+)$",
    re.I,
)
_OPEN = re.compile(r"^(?:open|view|read|bring\s+up|go\s+to|take\s+me\s+to)\s+(?P<desc>.+)$", re.I)
_REPO_ON_SITE = re.compile(
    r"^(?:open|go\s+to|take\s+me\s+to|show(?:\s+me)?|find|visit|check)\s+(?:my\s+|the\s+|our\s+|this\s+)?"
    r"(?:(?P<site1>github|gitlab|bitbucket)\s+)?(?:(?P<name>[\w.-]+)\s+)?(?:repo|repository|project)"
    r"(?:\s+on\s+(?P<site2>[\w.-]+))?$",
    re.I,
)
_PROJECT_NAMED = re.compile(
    r"^(?:open|go\s+to|take\s+me\s+to|switch\s+to|show(?:\s+me)?|find)\s+(?:my\s+|the\s+|our\s+)?"
    r"(?P<name>[\w.-]+(?:\s+[\w.-]+)?)\s+(?:project|repo|repository|codebase)$",
    re.I,
)
_BROWSER = re.compile(
    r"^(?P<verb>open|launch|start|bring\s+up|go\s+to|switch\s+to)\s+(?:up\s+)?(?:the\s+|my\s+|a\s+)?"
    r"(?:web\s+|default\s+|internet\s+)?browser$",
    re.I,
)
_STOP = frozenset(
    {
        *("the", "a", "an", "my", "our", "your", "this", "that", "these", "those", "it", "me", "i", "we"),
        *("about", "on", "of", "for", "called", "named", "with", "in", "from", "which", "regarding", "titled"),
        *("was", "were", "is", "are", "am", "some", "any", "all", "one", "ones", "latest", "newest"),
    }
)


# ----------------------------------------------------------------- clause result
@dataclass
class _Outcome:
    status: str
    intent: str = ""
    reason: str = ""
    question: str = ""
    steps: list[Step] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    ambiguity: Ambiguity | None = None
    unknown: Unknown | None = None


@dataclass
class _Request:
    """What the clauses of one request built so far."""

    entities: list[Entity] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    constraints: list[tuple[str, str]] = field(default_factory=list)
    earlier: list[list[Entity]] = field(default_factory=list)

    def entity(self, entity: Entity) -> int:
        for index, known in enumerate(self.entities):
            if (known.kind, known.value) == (entity.kind, entity.value):
                return index
        self.entities.append(entity)
        return len(self.entities) - 1

    def constrain(self, items: list[dict[str, str]]) -> None:
        for item in items:
            pair = (item["kind"], str(item["value"]))
            if pair not in self.constraints:
                self.constraints.append(pair)


@dataclass(frozen=True)
class Understood:
    """The HXIR, plus what the caller needs that is not serialised."""

    hxir: HXIR
    steps: tuple[Step, ...] = ()
    unknown: Unknown | None = None
    """The grammar's own explanation, when a clause named something it does not know."""


# --------------------------------------------------------------------- helpers
def _step(action: str, inputs: dict[str, object], description: str, target: str = "") -> Step:
    from highhx.actions.resolver import Step

    return Step(action, dict(inputs), description, target)


def project_entity(ctx: ResolverContext) -> Entity | None:
    if ctx.root is None:
        return None
    return Entity("project", ".", ctx.root.name, "project", "high", "HighhX is running in this project")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _first_word(text: str) -> str:
    return text.split(" ", 1)[0].lower() if text else ""


def _split_words() -> frozenset[str]:
    from highhx.actions.resolver import RULE_WORDS

    return RULE_WORDS | EXTRA_WORDS


def _verbs() -> frozenset[str]:
    from highhx.actions.resolver import RULE_WORDS
    from highhx.language.grammar import VERB_WORDS

    return frozenset(VERB_WORDS) | RULE_WORDS | EXTRA_WORDS


def _file_entity(found: FileEntity, evidence: str, confidence: str = "high") -> Entity:
    return Entity(
        "file",
        found.path,
        found.path,
        "project_index",
        confidence,
        evidence,
        (("modified", found.to_dict()["modified"]), ("size", str(found.size))),
    )


# ------------------------------------------------------------ before clauses
def _correction(request: str, memory: ConversationMemory | None) -> tuple[str, str] | _Outcome | None:
    """ "No, I meant GitHub" → ("open GitHub", the corrected request); None when it is not a correction."""
    m = _CORRECTION.match(request)
    new = m.group("new").strip(" ,") if m else None
    if new is None:
        m2 = _NO_THEN_REQUEST.match(request)
        if m2 is None or _first_word(m2.group("new")) not in _verbs():
            return None
        new = m2.group("new").strip()
    previous = memory.last_request if memory is not None else ""
    if _first_word(new) in _verbs():
        return new, previous  # "actually, open my repository": a whole new request
    if not previous:
        return _Outcome(MISSING, question=f"There is no earlier request here for {new!r} to correct. What should I do?")
    from highhx.language.grammar import split_clauses

    last = split_clauses(previous, _split_words())[-1]
    head = _VERB_HEAD.match(last)
    if head is None:
        return _Outcome(
            MISSING, question=f"I could not tell which part of {previous!r} to change. Say the whole request."
        )
    return f"{head.group(0)} {new}", previous


def _answer(request: str, memory: ConversationMemory | None) -> str | _Outcome | None:
    """An answer to "which one do you mean?" ("the second one", "2", "use docs/a.pdf") → the completed clause."""
    if memory is None or memory.pending is None:
        return None
    pending = memory.pending
    rest = _ANSWER.match(request).group("rest").strip()  # type: ignore[union-attr]
    chosen: Entity | None = None
    number = re.fullmatch(r"#?(\d{1,2})", rest)
    ref = detect(rest)
    if number is not None or (ref is not None and ref.kind == "ordinal"):
        index = int(number.group(1)) if number else (ref.ordinal if ref else 1) or 1
        index = len(pending.candidates) if index == -1 else index
        if not 1 <= index <= len(pending.candidates):
            return _Outcome(MISSING, question=f"There are only {len(pending.candidates)} to choose from.")
        chosen = pending.candidates[index - 1]
    else:
        chosen = next(
            (c for c in pending.candidates if rest.lower() in (c.value.lower(), c.shown.lower())),
            None,
        )
    if chosen is None:
        return None  # not an answer: a new request
    return pending.template.format(choice=chosen.value)


# ---------------------------------------------------------------- one clause
def _clause(
    text: str,
    ctx: ResolverContext,
    memory: ConversationMemory | None,
    state: PlanState,
    request: _Request,
    now: datetime,
) -> _Outcome:
    from highhx.actions.resolver import _resolve_one

    project = project_entity(ctx)
    here = _HERE.search(text)
    if here is not None and project is not None:  # "run the tests here": the project, which is where HighhX runs
        base = text[: here.start()].strip()
        found = _resolve_one(base, ctx)
        if found is not None:
            request.references.append(Reference(here.group(0).strip(), "place", RESOLVED, request.entity(project)))
            return _Outcome(RESOLVED, found[1], steps=[found[0]])
    deferred: _Outcome | None = None
    noted = len(request.references)
    split = split_reference(text)
    if split is not None and (_first_word(split[0]) in _verbs() or _BACK.match(split[0])):
        head, ref, tail = split
        outcome = _referenced(head, ref, tail, ctx, memory, state, request, project)
        if outcome is not None and outcome.status == RESOLVED:
            return outcome
        if outcome is not None and (_OPENING.match(head) or _BACK.match(head) or _USE.match(head)):
            return outcome  # "open it", "go back there": about the referent, which is unclear — ask
        deferred = outcome
    rest = _unreferenced(text, ctx, state, request, now)
    if deferred is None or rest.status in (RESOLVED, OPEN_ENDED):
        # "deploy this application" is not about an earlier entity: an unresolved reference only
        # explains a clause that nothing else resolves (or hands to HighhX Pro)
        del request.references[noted:]
        return rest
    return deferred


def _unreferenced(text: str, ctx: ResolverContext, state: PlanState, request: _Request, now: datetime) -> _Outcome:
    from highhx.actions.resolver import _resolve_one
    from highhx.language.grammar import Unknown, parse_clause

    found = _resolve_one(text, ctx)
    if found is not None:
        return _Outcome(RESOLVED, found[1], steps=[found[0]])
    parsed = parse_clause(text, ctx, state)
    if isinstance(parsed, list):
        return _Outcome(RESOLVED, parsed[0].action if parsed else "", steps=parsed)
    extended = (
        _browser(text, ctx, state)
        or _repository(text, ctx, state)
        or _project_named(text, ctx)
        or _files(text, ctx, request, now)
    )
    if extended is not None:
        return extended
    if isinstance(parsed, Unknown):
        return _Outcome(UNSUPPORTED, reason=parsed.reason, unknown=parsed)
    from highhx.agent.router import required_capability

    return _Outcome(OPEN_ENDED, reason=required_capability(text)[1])


def _referenced(
    head: str,
    ref: Ref,
    tail: str,
    ctx: ResolverContext,
    memory: ConversationMemory | None,
    state: PlanState,
    request: _Request,
    project: Entity | None,
) -> _Outcome | None:
    from highhx.actions.resolver import _resolve_one
    from highhx.language.grammar import parse_clause

    if memory is not None and memory.pending is not None and ref.kind == "pronoun":
        pending = memory.pending
        return _ambiguous(ref.text, f"{head} {{choice}}{tail}", pending.candidates, "Which one do you mean?")
    if _USE.match(head):
        return _Outcome(
            MISSING, question=f"What should I do with {ref.text!r}? Say it with a verb, e.g. open {ref.text}."
        )
    result = resolve_reference(ref, memory, request.earlier, project)
    if result.status == AMBIGUOUS:
        request.references.append(Reference(ref.text, ref.kind, AMBIGUOUS))
        return _ambiguous(ref.text, f"{head} {{choice}}{tail}", result.candidates, result.question)
    if result.status == MISSING or result.entity is None:
        on_page = state.site is not None or (memory is not None and memory.on_a_page and not memory.results)
        if ref.kind == "ordinal" and ref.noun in ("", "result", "link", "one", "item", "video") and on_page:
            return _Outcome(OPEN_ENDED, reason="Picking a result on the page needs the page to be read (HighhX Pro).")
        request.references.append(Reference(ref.text, ref.kind, MISSING))
        return _Outcome(MISSING, question=result.question)
    entity = result.entity
    request.references.append(Reference(ref.text, ref.kind, RESOLVED, request.entity(entity)))
    verb = "open" if _BACK.match(head) else head
    if not tail and _OPENING.match(verb):
        direct = _open_entity(entity, state)
        if direct is not None:
            return _Outcome(RESOLVED, direct.action, steps=[direct], entities=[entity])
    rewritten = f"{verb} {entity.value}{tail}"
    found = _resolve_one(rewritten, ctx)
    if found is not None:
        return _Outcome(RESOLVED, found[1], steps=[found[0]], entities=[entity])
    parsed = parse_clause(rewritten, ctx, state)
    if isinstance(parsed, list):
        return _Outcome(RESOLVED, parsed[0].action if parsed else "", steps=parsed, entities=[entity])
    return None  # the verb itself is not known here: the rest of the pipeline decides


def _open_entity(entity: Entity, state: PlanState) -> Step | None:
    from highhx.language.targets import default_registry

    if entity.kind in ("file", "folder"):
        return _step("filesystem.open", {"path": entity.value}, f"open {entity.value}", entity.value)
    if entity.kind == "project":
        return _step("filesystem.open", {"path": "."}, "open the project", "project")
    if entity.kind in ("url", "website", "repository"):
        site = default_registry().site_for_url(entity.value)
        state.site, state.surface = site, "browser"
        return _step("browser.open", {"url": entity.value}, f"open {entity.shown}", site.id if site else entity.value)
    if entity.kind == "application":
        return _step("computer.launch", {"name": entity.value}, f"open {entity.value}", entity.value)
    return None


def _ambiguous(text: str, template: str, candidates: tuple[Entity, ...], question: str) -> _Outcome:
    shown = candidates[:MAX_CANDIDATES]
    return _Outcome(
        AMBIGUOUS,
        reason=f"I found {len(candidates)} possible matches for {text!r}.",
        question=question or "Which one do you mean?",
        ambiguity=Ambiguity(text, question or "Which one do you mean?", shown, template),
    )


# -------------------------------------------------------- extended intents
def _repository(text: str, ctx: ResolverContext, state: PlanState) -> _Outcome | None:
    """ "open my repository on GitHub", "open my GitHub repo", "find my HighhX repository" (after
    "open GitHub"): the project's own repository page, from its git remotes."""
    m = _REPO_ON_SITE.match(text)
    if m is None:
        return None
    registry = ctx.targets()
    site_name = m.group("site1") or m.group("site2")
    site: Site | None = registry.site(site_name) if site_name else state.site
    if site_name and site is None:
        return _Outcome(UNSUPPORTED, reason=f"I don't know a website called {site_name!r}.")
    if site is None:
        return None  # "open my repository": the project folder (the grammar's project place)
    name = (m.group("name") or "").strip()
    if name.lower() in ("my", "the", "our", "this", "current"):
        name = ""
    remotes = [u for u in ctx.repositories() if (urlparse(u).hostname or "").removeprefix("www.") == site.host]
    if not remotes:
        return _Outcome(
            MISSING, question=f"This project has no {site.name} remote, so I don't know which repository you mean."
        )
    confidence, evidence = "high", f"the project's git remote on {site.name}"
    if name:
        exact = [u for u in remotes if _norm(u.rstrip("/").rsplit("/", 1)[-1]) == _norm(name)]
        partial = [u for u in remotes if _norm(name) in _norm(u.rstrip("/").rsplit("/", 1)[-1])]
        if exact:
            remotes = exact
        elif partial and len(_norm(name)) >= 3:
            remotes, confidence = partial, "medium"
            evidence = f"{name!r} is part of the repository name"
        else:
            listed = ", ".join(remotes)
            return _Outcome(
                MISSING,
                question=f"This project's {site.name} repository is {listed}; I don't know one called {name!r}.",
            )
    if len(remotes) > 1 and name:
        candidates = tuple(Entity("repository", u, u, "git_remote", "medium", evidence) for u in remotes)
        return _ambiguous(text, "open {choice}", candidates, "Which repository do you mean?")
    url = remotes[0]  # remotes are listed origin first
    if len(remotes) > 1:
        evidence += " (origin)"
    entity = Entity("repository", url, url, "git_remote", confidence, evidence)
    state.site, state.surface = site, "browser"
    step = _step("browser.open", {"url": url}, f"open {url}", site.id)
    return _Outcome(RESOLVED, "browser.open", steps=[step], entities=[entity])


def _browser(text: str, ctx: ResolverContext, state: PlanState) -> _Outcome | None:
    """ "start the browser": the browser installed on this computer — asked when there are several."""
    m = _BROWSER.match(text)
    if m is None:
        return None
    from highhx.computer.desktop import app_installed

    browsers = sorted(
        {a.name: a for a in ctx.targets().apps.values() if a.kind == "browser"}.values(), key=lambda a: a.name
    )
    installed = [a for a in browsers if app_installed(a.platform_name)]
    if not installed:
        return _Outcome(MISSING, question="I could not find an installed web browser. Which application should I open?")
    focus = m.group("verb").lower().startswith("switch")
    if len(installed) > 1:
        candidates = tuple(
            Entity("application", a.name, a.name, "installed_apps", "medium", "installed") for a in installed
        )
        verb = "switch to" if focus else "open"
        return _ambiguous(text, verb + " {choice}", candidates, "Which browser do you mean?")
    app = installed[0]
    entity = Entity("application", app.name, app.name, "installed_apps", "high", "the only browser installed")
    state.app, state.surface = app, "browser" if app.automatable else "desktop"
    action, key = ("computer.focus", "app") if focus else ("computer.launch", "name")
    step = _step(action, {key: app.name}, f"{'switch to' if focus else 'open'} {app.name}", app.id)
    return _Outcome(RESOLVED, action, steps=[step], entities=[entity])


def project_names(ctx: ResolverContext) -> dict[str, str]:
    """Names the current project goes by → where each comes from (folder, git remote)."""
    if ctx.root is None:
        return {}
    names = {ctx.root.name: "the project folder's name"}
    for url in ctx.repositories():
        names.setdefault(url.rstrip("/").rsplit("/", 1)[-1], f"the git remote {url}")
    return names


def _project_named(text: str, ctx: ResolverContext) -> _Outcome | None:
    """ "open my HighhX project": the current project, when that is its name (no other project is known)."""
    m = _PROJECT_NAMED.match(text)
    if m is None:
        return None
    name = m.group("name").strip()
    if name.lower() in ("current", "this", "my", "the", "our", "same", "whole", "entire"):
        return None
    names = project_names(ctx)
    if not names:
        return _Outcome(UNSUPPORTED, reason="HighhX is not running in a project here.")
    wanted = _norm(name)
    exact = [(n, why) for n, why in names.items() if _norm(n) == wanted]
    partial = [(n, why) for n, why in names.items() if len(wanted) >= 3 and (wanted in _norm(n) or _norm(n) in wanted)]
    if exact:
        confidence, evidence = "high", f"{exact[0][0]!r} is {exact[0][1]}"
    elif partial:
        confidence, evidence = "medium", f"{name!r} is part of {partial[0][0]!r} ({partial[0][1]})"
    else:
        assert ctx.root is not None
        known = " / ".join(sorted(names))
        return _Outcome(
            MISSING,
            question=f"HighhX only knows the project it is running in ({known}), not one called {name!r}. "
            "Run HighhX in that project's folder.",
        )
    entity = Entity("project", ".", ctx.root.name if ctx.root else ".", "project", confidence, evidence)
    step = _step("filesystem.open", {"path": "."}, "open the project", "project")
    return _Outcome(RESOLVED, "filesystem.open", steps=[step], entities=[entity])


def _topic(text: str) -> tuple[str, ...]:
    words = re.findall(r"[\w.-]+", text.lower())
    nouns = set(cons.FILE_NOUNS) | {n + "s" for n in cons.FILE_NOUNS}
    keep = [w for w in words if w not in _STOP and w not in nouns]
    return tuple(dict.fromkeys(keep))


def _files(text: str, ctx: ResolverContext, request: _Request, now: datetime) -> _Outcome | None:
    """ "find my PDF", "show me the latest report", "open the architecture document": project files."""
    find = _FIND.match(text)
    match = find or _OPEN.match(text)
    if match is None:
        return None
    desc = match.group("desc")
    constraints, rest = cons.extract(desc)
    if not constraints.file_noun:
        return None
    request.constrain(constraints.to_list())
    if constraints.downloaded:
        return _Outcome(
            UNSUPPORTED,
            reason="Files you downloaded are in your Downloads folder, outside this project. HighhX's file "
            "actions only reach files inside the project (the confinement policy), so it does not look there.",
        )
    if ctx.root is None:
        return _Outcome(UNSUPPORTED, reason="HighhX is not running in a project, and it only searches project files.")
    from highhx.project.index import FileQuery

    keywords = _topic(rest)
    window = constraints.window(now)
    if not (constraints.kinds or keywords or window):
        # "open the second file", "the file I was working on": which list, which file? Not a search
        # of the whole project by date — that would be a guess.
        return _Outcome(
            MISSING, question=f"Which file do you mean by {desc.strip()!r}? Give its name, type or when it changed."
        )
    sort = "oldest" if constraints.ordering == "oldest" else "newest"
    query = FileQuery(
        kinds=constraints.kinds,
        keywords=keywords,
        modified_after=window[0] if window else None,
        modified_before=window[1] if window else None,
        sort=sort,
        limit=MAX_CANDIDATES * 2,
    )
    found = ctx.files().find(query)
    by_order = constraints.ordering is not None or constraints.working_on
    pick: FileEntity | None = None
    why = ""
    if constraints.ordinal is not None:
        index = len(found) - 1 if constraints.ordinal == -1 else constraints.ordinal - 1
        if 0 <= index < len(found):
            pick, why = found[index], f"number {index + 1} of {len(found)} matching files, {sort} first"
    elif by_order and found:
        pick, why = found[0], f"the {sort} of {len(found)} matching file(s)"
    elif len(found) == 1:
        pick, why = found[0], "the only matching file"
    described = desc.strip()
    if find is not None:
        if constraints.ordinal is not None and constraints.ordinal > 0:
            limit = constraints.ordinal  # "find the second pdf": the list up to it
        elif pick is not None and constraints.ordinal is None:
            limit = 1  # "the latest report", or the only match: exactly the file "it" will mean
        else:
            limit = MAX_CANDIDATES * 2
        inputs: dict[str, object] = {"sort": sort, "limit": limit}
        if constraints.kinds:
            inputs["kinds"] = list(constraints.kinds)
        if keywords:
            inputs["keywords"] = list(keywords)
        if window:
            inputs["modified_after"] = datetime.fromtimestamp(window[0]).isoformat(timespec="seconds")
            inputs["modified_before"] = datetime.fromtimestamp(window[1]).isoformat(timespec="seconds")
        entities = (
            [_file_entity(pick, why)]
            if pick is not None
            else [_file_entity(f, "a matching file", "medium") for f in found]
        )
        step = _step("filesystem.find", inputs, f"find {described}", "project")
        return _Outcome(RESOLVED, "filesystem.find", steps=[step], entities=entities)
    if not found:
        return _Outcome(MISSING, question=f"No file in this project matches {described!r}.")
    if pick is None:
        candidates = tuple(_file_entity(f, "a matching file", "medium") for f in found)
        return _ambiguous(described, "open {choice}", candidates, "Which one do you mean?")
    entity = _file_entity(pick, why, "high" if keywords or constraints.kinds else "medium")
    step = _step("filesystem.open", {"path": pick.path}, f"open {pick.path}", pick.path)
    return _Outcome(RESOLVED, "filesystem.open", steps=[step], entities=[entity])


# ------------------------------------------------------------------ pipeline
def _goal_type(steps: list[Step], entities: list[Entity], status: str) -> str:
    if status != RESOLVED:
        return {AMBIGUOUS: "clarify", MISSING: "clarify", OPEN_ENDED: "open_ended"}.get(status, "unknown")
    if len(steps) != 1:
        return "multi_step"
    action = steps[0].action
    kinds = {e.kind for e in entities}
    if action == "browser.open":
        return "open_repository" if "repository" in kinds else "open_website"
    if action == "filesystem.open":
        return "open_project" if steps[0].inputs.get("path") == "." else "open_file"
    return {
        "computer.launch": "open_application",
        "computer.focus": "switch_application",
        "filesystem.find": "find_files",
        "browser.search": "web_search",
        "project.test": "run_tests",
    }.get(action, action.replace(".", "_"))


def understand(
    text: str,
    ctx: ResolverContext,
    memory: ConversationMemory | None = None,
    *,
    catalog: Catalog | None = None,
    now: datetime | None = None,
) -> Understood:
    """The HXIR for ``text`` in this project and conversation (deterministic: no model)."""
    from highhx.actions.resolver import MAX_CLAUSES, _drop_redundant_launches
    from highhx.language.grammar import PlanState, split_clauses

    if catalog is None:
        from highhx.actions.catalog import default_catalog

        catalog = default_catalog()
    now = now or datetime.now()
    request = normalise(text)
    correction_of = ""
    corrected = _correction(request, memory)
    if isinstance(corrected, _Outcome):
        return _finish(request, [(request, corrected)], _Request(), [], correction_of)
    if corrected is not None:
        request, correction_of = normalise(corrected[0]), corrected[1]
    answered = _answer(request, memory)
    if isinstance(answered, _Outcome):
        return _finish(request, [(request, answered)], _Request(), [], correction_of)
    if answered is not None:
        request = normalise(answered)
    forbid, body = cons.extract_forbidden(request)
    built = _Request()
    built.constrain([{"kind": "forbid", "value": f} for f in sorted(forbid)])
    clauses = split_clauses(body, _split_words())
    if len(clauses) > MAX_CLAUSES:
        outcome = _Outcome(OPEN_ENDED, reason=f"More than {MAX_CLAUSES} steps in one request.")
        return _finish(request, [(request, outcome)], built, [], correction_of)
    state = PlanState()
    done: list[tuple[str, _Outcome]] = []
    steps: list[Step] = []
    for raw in clauses:
        part = normalise(raw)
        outcome = _clause(part, ctx, memory, state, built, now)
        done.append((part, outcome))
        if outcome.status != RESOLVED:
            break  # all or nothing, as the resolver: later clauses may depend on this one
        produced = list(outcome.entities) or [e for step in outcome.steps for e in entities_of_step(step)[0]]
        for entity in produced:
            built.entity(entity)
        built.earlier.append(produced)
        steps += outcome.steps
    steps = _drop_redundant_launches(steps)
    return _finish(request, done, built, steps, correction_of, catalog=catalog, forbid=forbid, total=len(clauses))


def _finish(
    request: str,
    done: list[tuple[str, _Outcome]],
    built: _Request,
    steps: list[Step],
    correction_of: str,
    *,
    catalog: Catalog | None = None,
    forbid: frozenset[str] = frozenset(),
    total: int = 1,
) -> Understood:
    failed = next((o for _, o in done if o.status != RESOLVED), None)
    status = failed.status if failed is not None else RESOLVED
    reason = failed.reason if failed is not None else ""
    question = failed.question if failed is not None else ""
    actions = [
        HXAction(f"a{i}", s.action, dict(s.inputs), s.description, s.target, (f"a{i - 1}",) if i > 1 else ())
        for i, s in enumerate(steps, start=1)
    ]
    if status == RESOLVED and catalog is not None:
        broken = violations([(a.id, a.action) for a in actions], forbid, catalog)
        if broken:
            status, reason = INVALID, "The request contradicts itself: " + "; ".join(broken) + "."
    if status == RESOLVED and not actions:
        status, reason = INVALID, "Nothing to do: the request asks for no action."
    if total > len(done) and failed is not None:
        reason = (reason + " " if reason else "") + f"({total - len(done)} later step(s) not examined.)"
    ambiguities = tuple(o.ambiguity for _, o in done if o.ambiguity is not None)
    clauses = tuple(Clause(text, o.status, o.intent, o.reason or o.question) for text, o in done)
    hxir = HXIR(
        request=request,
        status=status,
        goal_type=_goal_type(steps, built.entities, status),
        goal_description=request,
        clauses=clauses,
        entities=tuple(built.entities),
        constraints=tuple(built.constraints),
        references=tuple(built.references),
        actions=tuple(actions) if status == RESOLVED else (),
        ambiguities=ambiguities,
        question=question,
        reason=reason.strip(),
        correction_of=correction_of,
    )
    return Understood(hxir, tuple(steps) if status == RESOLVED else (), failed.unknown if failed else None)


def from_resolution(request: str, steps: list[Step]) -> HXIR:
    """The HXIR of a request the existing resolver resolved on its own (same steps, as data)."""
    built = _Request()
    for step in steps:
        for entity in entities_of_step(step)[0]:
            built.entity(entity)
    forbid, _ = cons.extract_forbidden(request)
    built.constrain([{"kind": "forbid", "value": f} for f in sorted(forbid)])
    return _finish(request, [(request, _Outcome(RESOLVED, steps[0].action if steps else ""))], built, steps, "").hxir
