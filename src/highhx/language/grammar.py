"""The verb grammar: plain-language automation clauses → actions, deterministically.

    "open youtube and play adhento gani song"
      clauses   ["open youtube", "play adhento gani song"]      (split only before a known verb)
      open      target "youtube" → site YouTube                 → browser.open(https://www.youtube.com)
      play      query "adhento gani" (+ "song" dropped),
                site from the previous clause                   → browser.search(YouTube, "adhento gani")
                                                                  browser.play(YouTube, "adhento gani")

Each verb is a registry entry (words + a full-clause pattern + a handler); each handler
extracts its entities with fixed rules (:mod:`highhx.language.entities`), looks names up in
the target registry (:mod:`highhx.language.targets`) and returns steps, an :class:`Unknown`
that says what it did not recognise, or None when the clause needs understanding (Pro).
Nothing is inferred by similarity; nothing unrecognised is executed.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from highhx.language.entities import (
    BY_SUFFIX,
    DESCRIPTION,
    GENERIC_BROWSER,
    INTERPRETERS,
    as_url,
    key_combo,
    looks_like_path,
    media_query,
    query_of,
    split_in,
)
from highhx.language.targets import App, Site, TargetRegistry, normalise_name

if TYPE_CHECKING:
    from highhx.actions.resolver import ResolverContext, Step


@dataclass(frozen=True)
class Unknown:
    """A clause HighhX understood only partly — never executed, always explained."""

    clause: str
    reason: str
    suggestions: tuple[str, ...] = ()


@dataclass
class PlanState:
    """What earlier clauses established (carried to later ones)."""

    site: Site | None = None
    app: App | None = None
    surface: str = "browser"
    """Where UI verbs (click, type, press) act: the HighhX ``browser`` or the frontmost ``desktop`` app."""


Handler = Callable[[re.Match[str], "ResolverContext", PlanState], "list[Step] | Unknown | None"]


@dataclass(frozen=True)
class Verb:
    name: str
    pattern: re.Pattern[str]
    handler: Handler = field(compare=False)
    words: tuple[str, ...] = ()


VERBS: list[Verb] = []
VERB_WORDS: set[str] = set()


def verb(name: str, words: tuple[str, ...], pattern: str) -> Callable[[Handler], Handler]:
    """Register a verb: its leading words (for clause splitting) and a full-clause pattern."""

    def register(handler: Handler) -> Handler:
        VERBS.append(Verb(name, re.compile(pattern, re.IGNORECASE), handler, words))
        VERB_WORDS.update(words)
        return handler

    return register


def _step(action: str, inputs: dict[str, Any], description: str, target: str = "") -> Step:
    from highhx.actions.resolver import Step

    return Step(action, inputs, description, target)


def _registry(ctx: ResolverContext) -> TargetRegistry:
    return ctx.targets()


def _app_named(ctx: ResolverContext, name: str | None) -> App | Unknown | None:
    if not name:
        return None
    app = _registry(ctx).app(name)
    if app is None:
        return Unknown(name, f"I don't know an application called {name!r}.", ("/tools computer",))
    return app


def _existing(ctx: ResolverContext, raw: str) -> str | None:
    from highhx.actions.handlers.computer import project_path

    if ctx.root is None:
        return None
    path = project_path(ctx.root, raw)
    if path is None:
        return None
    rel = path.relative_to(ctx.root.resolve()).as_posix()
    return rel or "."


def _place(ctx: ResolverContext, text: str) -> tuple[str, str] | None:
    """A project place ("my project", "the readme", "the source folder") → (its id, its path)."""
    found = _registry(ctx).place(text, ctx.root)
    return (found[0].id, found[1]) if found is not None else None


def _repository_on(ctx: ResolverContext, site: Site) -> str | None:
    """The project's repository page on ``site`` (a git remote on the same host), if it has one."""
    host = site.host
    return next((url for url in ctx.repositories() if (urlparse(url).hostname or "") == host), None)


_NEW_TAB = re.compile(r"(?:a\s+)?new\s+tab(?:\s+(?:with|at|to|for|on)\s+(?P<where>.+))?", re.IGNORECASE)


# ---------------------------------------------------------------------- verbs
@verb(
    "open",
    ("open", "launch", "go", "visit", "navigate", "browse", "load", "start"),
    r"(?:open|launch|go\s+to|visit|navigate\s+to|browse\s+to|load|start)\s+(?:up\s+)?(?P<target>.+)",
)
def _open(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown | None:
    registry = _registry(ctx)
    whole = m.group("target").strip()
    tab = _NEW_TAB.fullmatch(whole)
    if tab is not None:
        state.surface = "browser"
        where = (tab.group("where") or "").strip()
        if not where:
            return [_step("browser.new_tab", {}, "open a new tab")]
        site = registry.site(where)
        url = site.url if site is not None else as_url(where)
        if url is None:
            return Unknown(where, f"I don't know a website called {where!r}.", ("open a new tab with example.com",))
        state.site = site or registry.site_for_url(url)
        return [_step("browser.new_tab", {"url": url}, f"open {site.name if site else url} in a new tab", url)]
    place = _place(ctx, whole)
    if place is not None and place[1] == "." and state.site is not None:
        repository = _repository_on(ctx, state.site)
        if repository is not None:  # "open github and open my repository": the project's page there
            return [_step("browser.open", {"url": repository}, f"open {repository}", state.site.id)]
    if place is not None:
        where_id, place_path = place
        shown = "the project" if place_path == "." else place_path
        return [_step("filesystem.open", {"path": place_path}, f"open {shown}", where_id)]
    target, where = split_in(whole)
    if where is not None and GENERIC_BROWSER.fullmatch(where):
        where = None  # "in the browser": HighhX's own browser
    if where is not None and registry.app(where) is None and DESCRIPTION.match(where):
        return None  # "… in the browser and fill in the form": more than this grammar knows
    app = _app_named(ctx, where)
    if isinstance(app, Unknown):
        return app
    extra = {"app": app.name} if app is not None else {}
    shown_in = f" in {app.name}" if app else ""
    place = _place(ctx, target)
    if place is not None:
        return [_step("filesystem.open", {"path": place[1], **extra}, f"open {place[1]}{shown_in}", place[0])]
    known = registry.site(target) if "://" not in target else None  # "youtube.com": the site's own address
    url = as_url(target) if known is None and not _existing(ctx, target) else None
    if url is not None:
        site = registry.site_for_url(url)
        state.site, state.surface = site, "browser"
        if app is not None:
            state.app = app
        return [_step("browser.open", {"url": url, **extra}, f"open {url}{shown_in}", site.id if site else url)]
    site = registry.site(target)
    if site is not None:
        state.site, state.surface = site, "browser"
        if app is not None:
            state.app = app
        return [_step("browser.open", {"url": site.url, **extra}, f"open {site.name}{shown_in}", site.id)]
    path = _existing(ctx, target)
    if path is not None:
        return [_step("filesystem.open", {"path": path, **extra}, f"open {path}{shown_in}", path)]
    named = registry.app(target)
    if named is not None and app is None:
        state.app, state.site = named, None
        state.surface = "browser" if named.automatable else "desktop"
        return [_step("computer.launch", {"name": named.name}, f"open {named.name}", named.id)]
    if looks_like_path(target):
        return Unknown(target, f"There is no file or folder {target!r} in this project.", ("create a file called …",))
    if DESCRIPTION.match(target):
        return None  # "the admin page", "my latest invoice": a description needs understanding (Pro)
    return Unknown(
        target,
        f"I don't know an app or website called {target!r} yet.",
        ("open a URL (open example.com)", "add it to targets.yaml", "/tools browser"),
    )


def _search_site(ctx: ResolverContext, state: PlanState, site_name: str | None) -> Site | Unknown | None:
    if site_name:
        site = _registry(ctx).site(site_name)
        if site is None:
            return None
        if site.search is None:
            return Unknown(site_name, f"I don't know how to search {site.name} yet.", ("search the web for …",))
        return site
    if state.site is not None and state.site.search is not None:
        return state.site
    return None


def _search_steps(query: str, site: Site | None, state: PlanState) -> list[Step]:
    inputs: dict[str, Any] = {"query": query}
    app = state.app if state.app and state.app.browser else None
    if site is not None:
        inputs["site"] = site.name
        state.site = site
    if app is not None and not app.automatable:
        inputs["app"] = app.name
    state.surface = "browser"
    where = site.name if site else "the web"
    shown_in = f" in {inputs['app']}" if "app" in inputs else ""
    return [_step("browser.search", inputs, f"search {where} for {query!r}{shown_in}", site.id if site else "web")]


_WEB = ("the web", "web", "online", "internet", "the internet")
MAX_BARE_QUERY_WORDS = 6
"""'search internship' is a search; a long sentence after 'search' is a task description."""


@verb(
    "search",
    ("search", "look", "find"),
    r"(?:search|look\s+up|find)\s+(?:"
    r"(?P<site1>[\w .-]+?)\s+for\s+(?P<q1>.+)"
    r"|(?:the\s+web\s+|online\s+|internet\s+)?for\s+(?P<q2>.+?)(?:\s+on\s+(?P<site2>[\w .-]+))?"
    r"|(?P<q3>.+?)\s+on\s+(?P<site3>[\w .-]+)"
    r"|(?P<q4>.+))",
)
def _search(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown | None:
    if m.group("q4") is not None and (
        not m.group(0).lower().startswith("search") or len(m.group("q4").split()) > MAX_BARE_QUERY_WORDS
    ):
        return None  # "find the most important email from last week …": understanding, not a search
    if m.group("site1") and DESCRIPTION.match(m.group("site1")) and _registry(ctx).site(m.group("site1")) is None:
        return None  # "search my emails for the invoice …": a description, not a site
    raw_site = m.group("site1") or m.group("site2") or m.group("site3")
    query = query_of(m.group("q1") or m.group("q2") or m.group("q3") or m.group("q4") or "")
    if raw_site and normalise_name(raw_site) in _WEB:
        raw_site = None
    site = _search_site(ctx, state, raw_site)
    if isinstance(site, Unknown):
        return site
    if raw_site and site is None:
        if m.group("site1"):  # "search python jobs for beginners": not a site — all of it is the query
            query = query_of(f"{m.group('site1')} for {m.group('q1')}")
        else:
            return Unknown(raw_site, f"I don't know a website called {raw_site!r} yet.", ("add it to targets.yaml",))
    if not query:
        return Unknown(m.group(0), "Search for what?", ('search for "…"',))
    return _search_steps(query, site, state)


@verb("google", ("google",), r"google\s+(?:for\s+)?(?P<q>.+)")
def _google(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown:
    return _search_steps(query_of(m.group("q")), _registry(ctx).site("google"), state)


@verb("play", ("play", "watch", "listen"), r"(?:play|watch|listen\s+to)\s+(?P<q>.+?)(?:\s+on\s+(?P<site>[\w .-]+))?")
def _play(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown:
    registry = _registry(ctx)
    site = registry.site(m.group("site")) if m.group("site") else state.site or registry.site("youtube")
    if site is None or site.play is None:
        name = m.group("site") or (state.site.name if state.site else "")
        return Unknown(
            m.group(0), f"I can't play media on {name!r} yet — YouTube is supported.", ("play … on youtube",)
        )
    query = media_query(m.group("q"))
    if not query:
        return Unknown(m.group(0), "Play what?", ("play <title> on youtube",))
    if state.app is not None and not state.app.automatable and state.app.browser:
        return Unknown(
            m.group(0),
            f"HighhX can't operate {state.app.name}; playing needs the HighhX browser (Chrome, Edge or Brave).",
            (f"play {query} on youtube",),
        )
    state.site, state.surface = site, "browser"
    return [
        _step("browser.search", {"query": query, "site": site.name}, f"search {site.name} for {query!r}", site.id),
        _step("browser.play", {"query": query, "site": site.name}, f"play the first {site.name} result", site.id),
    ]


@verb(
    "click",
    ("click", "tap"),
    r"(?:click|tap)\s+(?:on\s+)?(?:the\s+)?[\"']?(?P<name>[^\"']+?)[\"']?(?:\s+(?P<role>button|link|tab|checkbox|menu\s*item|field))?",
)
def _click(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown:
    role = (m.group("role") or "button").replace(" ", "").lower()
    name = m.group("name").strip()
    if state.surface == "desktop":
        return [_step("computer.click", {"target": f"{role}:{name}"}, f"click {role} {name!r}", name)]
    return [_step("browser.click", {"target": f"{role}:{name}"}, f"click {role} {name!r}", name)]


@verb(
    "type",
    ("type", "enter", "write"),
    r"(?:type|enter|write)\s+(?P<text>[\"'“].+[\"'”]|.+?)(?:\s+(?:in|into)\s+(?:the\s+)?(?P<field>[\w ]+?)(?:\s+(?:field|box))?)?",
)
def _type(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown:
    text = query_of(m.group("text"))
    if state.surface == "browser" and m.group("field"):
        field_name = m.group("field").strip()
        return [
            _step(
                "browser.fill", {"target": f"textbox:{field_name}", "text": text}, f"type into {field_name}", field_name
            )
        ]
    if state.surface == "browser":
        return Unknown(m.group(0), 'Type into which field? e.g. type "hello" into search', ())
    return [_step("computer.type", {"text": text}, f"type {len(text)} character(s)", state.app.id if state.app else "")]


_BROWSER_KEYS = {
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "escape": "escape",
    "esc": "escape",
    "space": "space",
    "backspace": "backspace",
    "arrowdown": "arrowdown",
    "arrowup": "arrowup",
    "down": "arrowdown",
    "up": "arrowup",
    "pagedown": "pagedown",
    "pageup": "pageup",
}


@verb("press", ("press", "hit"), r"(?:press|hit)\s+(?:the\s+)?(?P<keys>[\w+ -]+?)(?:\s+key)?")
def _press(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown:
    combo = key_combo(m.group("keys"))
    if combo is None:
        return Unknown(m.group(0), f"I don't know the key {m.group('keys')!r}.", ("press cmd+t", "press enter"))
    modifiers, key = combo
    if modifiers:
        keys = "+".join([*modifiers, key])
        return [_step("computer.hotkey", {"keys": keys}, f"press {keys}")]
    if state.surface == "browser" and key in _BROWSER_KEYS:
        return [_step("browser.press", {"key": _BROWSER_KEYS[key]}, f"press {_BROWSER_KEYS[key]}")]
    return [_step("computer.press", {"key": key}, f"press {key}")]


@verb("scroll", ("scroll",), r"scroll\s+(?P<dir>up|down)(?:\s+(?:the\s+)?page)?")
def _scroll(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown:
    direction = m.group("dir").lower()
    return [_step("computer.scroll", {"direction": direction, "source": state.surface}, f"scroll {direction}")]


_ELEMENT = r"(?:the\s+)?[\"']?(?P<name>[^\"']+?)[\"']?(?:\s+(?P<role>button|link|tab|checkbox|menu\s*item|field|item))?"


def _selector(m: re.Match[str], default_role: str = "button") -> tuple[str, str]:
    role = (m.group("role") or default_role).replace(" ", "").lower()
    name = m.group("name").strip()
    return f"{role}:{name}", name


@verb("history", ("back", "forward"), r"(?:go\s+)?(?P<dir>back|forward)(?:\s+(?:a|one)\s+page)?")
def _history(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step]:
    direction = m.group("dir").lower()
    return [_step(f"browser.{direction}", {}, f"go {direction}")]


@verb("refresh", ("refresh", "reload"), r"(?:refresh|reload)(?:\s+(?:the|this))?(?:\s+page)?")
def _refresh(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step]:
    return [_step("browser.refresh", {}, "reload the page")]


@verb("close tab", ("close",), r"close\s+(?:the\s+|this\s+|current\s+)*tab")
def _close_tab(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step]:
    return [_step("browser.close_tab", {}, "close the tab")]


@verb("switch tab", ("switch",), r"switch\s+to\s+(?:the\s+)?(?P<tab>.+?)\s+tab")
def _switch_tab(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step]:
    tab = m.group("tab").strip()
    site = _registry(ctx).site(tab)
    if site is not None:  # "the gmail tab": the site's host is what the tab's URL shows
        tab = site.url.split("//", 1)[-1].split("/", 1)[0].removeprefix("www.")
    state.surface = "browser"
    return [_step("browser.switch_tab", {"tab": tab}, f"switch to the {m.group('tab').strip()} tab", tab)]


@verb("hover", ("hover",), r"hover\s+(?:over\s+|on\s+)?" + _ELEMENT)
def _hover(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step]:
    target, name = _selector(m, "link")
    return [_step("browser.hover", {"target": target}, f"hover over {name!r}", name)]


@verb("double click", ("double", "double-click", "doubleclick"), r"double[\s-]?click\s+(?:on\s+)?" + _ELEMENT)
def _double_click(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step]:
    target, name = _selector(m)
    return [_step("browser.double_click", {"target": target}, f"double-click {name!r}", name)]


@verb("download", ("download",), r"download\s+" + _ELEMENT)
def _download(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step]:
    target, name = _selector(m, "link")
    return [_step("browser.download", {"target": target}, f"download {name!r}", name)]


@verb(
    "upload",
    ("upload", "attach"),
    r"(?:upload|attach)\s+(?P<files>.+?)\s+(?:to|into|in)\s+(?:the\s+)?[\"']?(?P<field>[^\"']+?)[\"']?(?:\s+field)?",
)
def _upload(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown:
    paths = [p.strip().strip("\"'`") for p in re.split(r"\s*(?:,|\band\b)\s*", m.group("files")) if p.strip()]
    missing = [p for p in paths if _existing(ctx, p) is None]
    if missing:
        return Unknown(m.group(0), f"There is no file {missing[0]!r} in this project.", ())
    field_name = m.group("field").strip()
    return [
        _step(
            "browser.upload",
            {"target": f"textbox:{field_name}", "paths": paths},
            f"upload {', '.join(paths)}",
            field_name,
        )
    ]


@verb(
    "drag",
    ("drag",),
    r"drag\s+(?:the\s+)?[\"']?(?P<source>[^\"']+?)[\"']?\s+(?:on)?to\s+(?:the\s+)?[\"']?(?P<target>[^\"']+?)[\"']?",
)
def _drag(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step]:
    source, target = m.group("source").strip(), m.group("target").strip()
    return [_step("browser.drag", {"source": source, "target": target}, f"drag {source!r} onto {target!r}", source)]


@verb(
    "focus",
    ("focus", "switch", "activate", "bring"),
    r"(?:focus(?:\s+on)?|switch\s+to|activate|bring\s+up)\s+(?P<app>.+)",
)
def _focus(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown:
    app = _app_named(ctx, m.group("app"))
    if isinstance(app, Unknown) or app is None:
        return app or Unknown(m.group(0), "Switch to which application?", ())
    state.app, state.surface = app, "browser" if app.automatable else "desktop"
    return [_step("computer.focus", {"app": app.name}, f"switch to {app.name}", app.id)]


@verb(
    "create",
    ("create", "make", "new", "add"),
    r"(?:create|make|add)\s+(?:a\s+|an\s+)?(?:new\s+)?(?P<kind>folder|directory|dir|file)\s+(?:called\s+|named\s+)?(?P<name>[\"'`]?[\w./ -]+?[\"'`]?)",
)
def _create(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown:
    name = query_of(m.group("name"))
    kind = "file" if m.group("kind").lower() == "file" else "folder"
    if not name or name.startswith("/") or ".." in Path(name).parts:
        return Unknown(m.group(0), "Give a name inside the project, e.g. create a folder called test.", ())
    if _existing(ctx, name) is not None:
        return Unknown(
            m.group(0),
            f"{name!r} already exists in this project — HighhX never overwrites it with an empty {kind}.",
            (f"open {name}",),
        )
    return [_step("filesystem.create", {"path": name, "kind": kind}, f"create {kind} {name}", name)]


@verb(
    "list",
    ("list", "show"),
    r"(?:list|show)\s+(?:the\s+|me\s+(?:the\s+|my\s+)?|my\s+|all\s+(?:the\s+)?)?(?:project\s+)?(?:files|folders|contents)(?:\s+(?:in|of)\s+(?P<path>.+))?",
)
def _list(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown:
    raw = m.group("path")
    place = _place(ctx, raw) if raw else None
    path = place[1] if place else (_existing(ctx, raw) if raw else ".")
    if path is None:
        return Unknown(m.group(0), f"There is no folder {raw!r} in this project.", ("show my files",))
    target = place[0] if place else ("project" if path == "." else path)
    return [_step("filesystem.list", {"path": path}, f"list {'the project' if path == '.' else path}", target)]


@verb(
    "run",
    ("run", "execute"),
    r"(?:run|execute)\s+(?:(?P<interp>python3?|node|ruby|bash|sh|deno)\s+)?(?P<file>[\w./-]+)(?P<args>\s+.+)?",
)
def _run(m: re.Match[str], ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown | None:
    """``run python hello.py`` — only an existing project file with a known interpreter; the command
    line is built by HighhX (never taken from the sentence) and classified like any other."""
    import shutil

    raw = m.group("file")
    typed = m.group(0).split(" ", 1)[1].strip()
    if m.group("args"):
        if m.group("interp") or shutil.which(raw):
            return Unknown(
                typed, "Shell commands with arguments run through !command, where they are classified.", ("!" + typed,)
            )
        return None  # "run a security audit and …" is a request, not a program: open-ended
    path = _existing(ctx, raw)
    if path is None or not (ctx.root is not None and (ctx.root / path).is_file()):
        if not looks_like_path(raw) and not m.group("interp"):
            if shutil.which(raw):
                return Unknown(typed, "Shell commands run through !command, where they are classified.", ("!" + typed,))
            return None
        return Unknown(m.group(0), f"There is no file {raw!r} in this project.", ())
    interp = INTERPRETERS.get((m.group("interp") or "").lower()) or BY_SUFFIX.get(Path(path).suffix.lower())
    if interp is None:
        return Unknown(m.group(0), f"I don't know which program runs {path!r}. Use !command.", ())
    command = f"{interp} {shlex.quote(path)}"
    return [_step("shell.run", {"command": command}, f"run {command}", path)]


def parse_clause(text: str, ctx: ResolverContext, state: PlanState) -> list[Step] | Unknown | None:
    """Steps for one clause, an :class:`Unknown` explanation, or None when no verb applies."""
    for entry in VERBS:
        match = entry.pattern.fullmatch(text)
        if match is not None:
            return entry.handler(match, ctx, state)
    return None


def split_clauses(text: str, extra_words: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Clauses of ``text`` (split before this grammar's verbs and ``extra_words``)."""
    from highhx.language.parser import split_clauses as split

    return split(text, VERB_WORDS | set(extra_words))
