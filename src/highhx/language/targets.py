"""What HighhX can open: websites, applications and project places — as data.

The built-in targets live in ``language/data/targets.yaml``; users add their own in
``<config dir>/targets.yaml`` in the same format:

    sites:
      - id: jira
        name: Jira
        aliases: [jira, tickets]
        url: https://example.atlassian.net
        capabilities: [open, search]
        search: https://example.atlassian.net/issues/?jql=text~"{query}"
    apps:
      - {id: obsidian, name: Obsidian, aliases: [obsidian]}

Adding a target is a registry entry, never parser code. Names are matched after normalisation
(lower case, "the"/"my" and ".com"/"app"/"website" dropped) and only exactly — never by
similarity.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

from highhx.computer.desktop import KNOWN_APPS, mac_app_dirs, resolve_app

BUILTIN_FILE = Path(__file__).parent / "data" / "targets.yaml"
TERMINAL_NAMES = frozenset({"terminal", "iterm", "iterm2", "warp", "alacritty", "kitty", "hyper", "wezterm", "ghostty"})
DEFAULT_SEARCH = "https://www.google.com/search?q={query}"
SITE_CAPABILITIES = frozenset({"open", "search", "play"})
APP_KINDS = frozenset({"app", "browser", "terminal", "editor", "files"})


@dataclass(frozen=True)
class Play:
    """How a site's media is played deterministically: open the results for the query, open the
    first result whose link matches ``result_href``, then start the page's media element."""

    results: str
    """Results URL template with ``{query}``."""
    result_href: str
    """Substring every playable result link contains (e.g. ``/watch?v=``)."""
    watch_url: str
    """Substring of the URL once a result is open (verification)."""


@dataclass(frozen=True)
class Site:
    name: str
    aliases: tuple[str, ...]
    url: str
    search: str | None = None
    """Search URL template with ``{query}`` (None: the site cannot be searched by URL)."""
    play: Play | None = None
    id: str = ""
    capabilities: tuple[str, ...] = ("open",)
    results: str = ""
    """Substring of the URL once search results show (verification); empty: the search URL itself."""
    signin: tuple[str, ...] = ()
    """URL prefixes (host or host/path) that mean the site wants the person to sign in."""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", _slug(self.name))
        caps = set(self.capabilities)
        if self.search:
            caps.add("search")
        if self.play:
            caps.add("play")
        object.__setattr__(self, "capabilities", tuple(sorted(caps | {"open"})))

    @property
    def host(self) -> str:
        return (urlparse(self.url).hostname or "").removeprefix("www.")

    def search_url(self, query: str) -> str:
        if self.search is None:
            raise ValueError(f"{self.name} has no search")
        return self.search.format(query=quote_plus(query))

    def wants_signin(self, url: str) -> bool:
        """``url`` is a sign-in page for this site (the person must sign in; HighhX never does)."""
        parsed = urlparse(url)
        where = f"{(parsed.hostname or '').removeprefix('www.')}{parsed.path}"
        return any(where.startswith(prefix) for prefix in self.signin)


@dataclass(frozen=True)
class App:
    name: str
    """Display name; the platform's application name comes from :func:`resolve_app`."""
    aliases: tuple[str, ...]
    browser: bool = False
    automatable: bool = False
    """A browser HighhX can drive (Chrome family, via DevTools). Others can only open URLs."""
    terminal: bool = False
    """A terminal emulator: HighhX never types or presses keys into it (use !command)."""
    id: str = ""
    kind: str = "app"

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", _slug(self.aliases[0] if self.aliases else self.name))

    @property
    def platform_name(self) -> str:
        return resolve_app(self.aliases[0]) if self.aliases[0] in KNOWN_APPS else self.name


@dataclass(frozen=True)
class Place:
    """A place in the current project ("my project", "the readme", "the source folder")."""

    id: str
    aliases: tuple[str, ...]
    path: str | None = None
    find: tuple[str, ...] = ()

    def locate(self, root: Path) -> str | None:
        """The place's path relative to ``root``, or None when the project has none."""
        if self.path is not None:
            return self.path
        for candidate in self.find:
            if (root / candidate).exists():
                return candidate
        return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _place_key(text: str) -> str:
    """ "the project folder" / "my project folder" / "project folder" are one place."""
    text = " ".join(text.strip().lower().split())
    return re.sub(r"^(?:the|my|our|this|current|the current)\s+", "", text)


def normalise_name(text: str) -> str:
    text = " ".join(text.strip().lower().split())
    text = re.sub(r"^(?:the|my|our|a)\s+", "", text)
    text = re.sub(r"\s+(?:app|application|website|site|web ?site|page|homepage)$", "", text)
    return text.strip(" .\"'")


@dataclass
class TargetRegistry:
    sites: dict[str, Site] = field(default_factory=dict)
    apps: dict[str, App] = field(default_factory=dict)
    places: dict[str, Place] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    """Entries of a targets file that were skipped (reported by `highhx computer status`)."""

    def add_site(self, site: Site) -> None:
        for alias in (site.name, site.id, *site.aliases):
            self.sites[normalise_name(alias)] = site
            if alias.endswith(".com"):
                self.sites[normalise_name(alias[: -len(".com")])] = site

    def add_app(self, app: App) -> None:
        for alias in (app.name, app.id, *app.aliases):
            self.apps[normalise_name(alias)] = app

    def add_place(self, place: Place) -> None:
        for alias in place.aliases:
            self.places[_place_key(alias)] = place

    def site(self, name: str) -> Site | None:
        return self.sites.get(normalise_name(name))

    def site_for_url(self, url: str) -> Site | None:
        host = (urlparse(url).hostname or "").removeprefix("www.")
        return next((s for s in self.sites.values() if s.host and s.host == host), None)

    def app(self, name: str) -> App | None:
        key = normalise_name(name)
        found = self.apps.get(key)
        if found is not None:
            return found
        if key in KNOWN_APPS:  # the computer layer's own table
            browser = key in ("chrome", "google chrome", "edge", "firefox", "safari")
            return App(resolve_app(key), (key,), browser=browser, kind="browser" if browser else "app")
        return _installed_app(name)

    def place(self, name: str, root: Path | None) -> tuple[Place, str] | None:
        """A project place and its path, when ``name`` names one that exists in ``root``."""
        if root is None:
            return None
        place = self.places.get(_place_key(name))
        if place is None:
            return None
        path = place.locate(root)
        return (place, path) if path is not None else None

    def known(self) -> dict[str, list[str]]:
        return {
            "apps": sorted({a.name for a in self.apps.values()}),
            "sites": sorted({s.name for s in self.sites.values()}),
            "places": sorted({p.id for p in self.places.values()}),
        }


def _installed_app(name: str) -> App | None:
    """An application installed under that exact name (macOS bundles) — never a partial match."""
    display = " ".join(name.strip().split())
    if sys.platform != "darwin" or not display or "/" in display:
        return None
    for base in mac_app_dirs():
        for candidate in (display, display.title()):
            if (Path(base) / f"{candidate}.app").exists():
                terminal = candidate.lower() in TERMINAL_NAMES
                return App(candidate, (candidate.lower(),), terminal=terminal, kind="terminal" if terminal else "app")
    return None


# ------------------------------------------------------------------ loading
def _strings(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str | int | float) for v in value):
        raise ValueError(f"{what} must be a list of names")
    return tuple(str(v) for v in value)


def _site(raw: dict[str, Any]) -> Site:
    name = str(raw["name"])
    url = str(raw["url"])
    if not url.startswith(("https://", "http://")):
        raise ValueError("url must start with http(s)://")
    search = raw.get("search")
    if search is not None and "{query}" not in str(search):
        raise ValueError("search must contain {query}")
    capabilities = _strings(raw.get("capabilities"), "capabilities") or ("open",)
    unknown = set(capabilities) - SITE_CAPABILITIES
    if unknown:
        raise ValueError(f"unknown capabilities {sorted(unknown)} (known: {sorted(SITE_CAPABILITIES)})")
    play = None
    if raw.get("play"):
        p = raw["play"]
        if "{query}" not in str(p["results"]):
            raise ValueError("play.results must contain {query}")
        play = Play(str(p["results"]), str(p["result_href"]), str(p["watch_url"]))
    return Site(
        name,
        _strings(raw.get("aliases"), "aliases") or (name.lower(),),
        url,
        str(search) if search else None,
        play,
        id=str(raw.get("id") or ""),
        capabilities=capabilities,
        results=str(raw.get("results") or ""),
        signin=_strings(raw.get("signin"), "signin"),
    )


def _app(raw: dict[str, Any]) -> App:
    name = str(raw["name"])
    kind = str(raw.get("kind") or "app")
    if kind not in APP_KINDS:
        raise ValueError(f"unknown kind {kind!r} (known: {sorted(APP_KINDS)})")
    return App(
        name,
        _strings(raw.get("aliases"), "aliases") or (name.lower(),),
        browser=kind == "browser",
        automatable=bool(raw.get("automatable")) and kind == "browser",
        terminal=kind == "terminal" or name.lower() in TERMINAL_NAMES,
        id=str(raw.get("id") or ""),
        kind=kind,
    )


def _place(raw: dict[str, Any]) -> Place:
    path = raw.get("path")
    find = _strings(raw.get("find"), "find")
    for candidate in [*(p for p in [path] if p is not None), *find]:
        parts = Path(str(candidate)).parts
        if Path(str(candidate)).is_absolute() or ".." in parts:
            raise ValueError("project places must stay inside the project")
    if path is None and not find:
        raise ValueError("a project place needs path or find")
    return Place(str(raw["id"]), _strings(raw.get("aliases"), "aliases"), None if path is None else str(path), find)


def load_targets(registry: TargetRegistry, path: Path) -> list[str]:
    """Add the sites, apps and project places in a targets file; returns problems (never raises)."""
    import yaml

    if not path.is_file():
        return []
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return [f"{path}: {exc}"]
    if not isinstance(data, dict):
        return [f"{path}: expected sites/apps/project sections"]
    problems: list[str] = []
    loaders: dict[str, Callable[[dict[str, Any]], None]] = {
        "sites": lambda raw: registry.add_site(_site(raw)),
        "apps": lambda raw: registry.add_app(_app(raw)),
        "project": lambda raw: registry.add_place(_place(raw)),
    }
    for section, load in loaders.items():
        entries = data.get(section) or []
        if not isinstance(entries, list):
            problems.append(f"{path}: {section} must be a list")
            continue
        for index, raw in enumerate(entries):
            try:
                if not isinstance(raw, dict):
                    raise ValueError("expected a mapping")
                load(raw)
            except (KeyError, TypeError, ValueError) as exc:
                problems.append(f"{path}: {section}[{index}]: {exc}")
    registry.problems += problems
    return problems


load_user_targets = load_targets  # the user's file has the same format


@cache
def _builtin() -> TargetRegistry:
    registry = TargetRegistry()
    problems = load_targets(registry, BUILTIN_FILE)
    if problems:  # a packaging bug, not a user error
        raise RuntimeError("; ".join(problems))
    return registry


def default_registry(*, user_file: Path | None = None) -> TargetRegistry:
    base = _builtin()
    registry = TargetRegistry(dict(base.sites), dict(base.apps), dict(base.places))
    if user_file is not None:
        load_targets(registry, user_file)
    return registry


def user_targets_file() -> Path:
    from highhx.utils.paths import user_config_dir

    return user_config_dir() / "targets.yaml"
