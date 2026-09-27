"""Deterministic intent resolution: plain language → actions, without AI.

A request resolves only when it matches the command grammar *completely* and every entity
in it is known — a configured service, deploy target, environment or workflow, a file that
exists, a URL, a quoted commit message. Anything else is unresolved (on Free that means
HighhX Pro; on Pro the AI agent takes it). No model, no network, no fuzzy guessing: the
same text and project always give the same result.

Compound requests ("run the tests then build") resolve to several steps only when every
part resolves on its own.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote_plus

from highhx.computer.intents import BROWSERS, DEFAULT_SEARCH_URL, looks_like_file

if TYPE_CHECKING:
    from highhx.commands import App

COMMON_ENVIRONMENTS = ("dev", "development", "test", "staging", "stage", "qa", "preview", "production", "prod")
SERVICE_WORDS = ("server", "backend", "frontend", "api", "web", "app", "worker", "db", "database", "services")


@dataclass(frozen=True)
class Step:
    action: str
    inputs: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class Resolution:
    steps: tuple[Step, ...]
    text: str
    rule: str
    """Which grammar rule matched (for tests, /plan and the event log)."""

    @property
    def action(self) -> str:
        return self.steps[0].action

    @property
    def inputs(self) -> dict[str, Any]:
        return self.steps[0].inputs

    @property
    def description(self) -> str:
        return " → ".join(s.description or s.action for s in self.steps)


@dataclass
class ResolverContext:
    """Entities the resolver may use (names only — never values)."""

    root: Path | None = None
    services: tuple[str, ...] = ()
    deploy_targets: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()

    @classmethod
    def from_app(cls, app: App) -> ResolverContext:
        from highhx.core.errors import HighhXError

        workflows: tuple[str, ...] = ()
        environments: tuple[str, ...] = ()
        try:
            workflows = tuple(sorted(ref.key for ref in app.workflow_loader.list()))
        except HighhXError:
            pass
        if app.initialized:
            try:
                environments = tuple(app.environment.profile_names())
            except HighhXError:
                pass
        return cls(
            root=app.root,
            services=tuple(sorted(app.config.services)),
            deploy_targets=tuple(sorted(app.config.deploy_targets)),
            environments=environments,
            workflows=workflows,
        )

    def environment(self, word: str) -> str | None:
        """A known deploy target / environment name (``prod`` finds ``production``)."""
        word = word.strip().lower()
        known = [*self.deploy_targets, *self.environments]
        for name in known:
            if name.lower() == word:
                return name
        aliases = {
            "prod": "production",
            "production": "prod",
            "stage": "staging",
            "staging": "stage",
            "dev": "development",
        }
        alias = aliases.get(word)
        for name in known:
            if alias and name.lower() == alias:
                return name
        if not known and word in COMMON_ENVIRONMENTS:
            return word  # no targets configured: `highhx deploy` reports what is missing
        return None

    def service(self, word: str) -> list[str] | None:
        """Configured services named by ``word`` (``[]`` = all services); None when unknown."""
        word = word.strip().lower()
        if word in ("", "services", "all services", "everything", "all"):
            return []
        exact = [s for s in self.services if s.lower() == word]
        if exact:
            return exact
        partial = [s for s in self.services if word in s.lower()]
        if len(partial) == 1:
            return partial
        if word in ("server", "servers") and self.services:
            return []
        return None

    def workflow(self, word: str) -> str | None:
        word = word.strip().lower()
        return next((w for w in self.workflows if w.lower() == word), None)

    def existing_file(self, raw: str) -> str | None:
        if self.root is None:
            return None
        candidate = raw.strip().strip("\"'`")
        path = (self.root / candidate).resolve()
        root = self.root.resolve()
        if path != root and path.is_relative_to(root) and path.exists():
            return candidate
        return None


# --------------------------------------------------------------------- grammar
class MatchLike(Protocol):
    def group(self, name: str | int, /) -> Any: ...

    def groupdict(self) -> dict[str, Any]: ...


Builder = Callable[[MatchLike, ResolverContext], Step | None]


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    build: Builder


def _rule(name: str, pattern: str, build: Builder) -> Rule:
    return Rule(name, re.compile(rf"(?:{pattern})", re.IGNORECASE), build)


def _fixed(action: str, description: str, **inputs: Any) -> Builder:
    return lambda _m, _c: Step(action, dict(inputs), description)


THE = r"(?:the\s+|my\s+|our\s+|this\s+)?"
RUN = r"(?:run\s+|execute\s+|start\s+|do\s+)?"
SHOW = r"(?:show\s+(?:me\s+)?|display\s+|print\s+|list\s+|view\s+|get\s+|check\s+|what(?:'s| is)\s+)?"
PROJECT = r"(?:\s+(?:the\s+|this\s+)?(?:project|app|application|code|repo|repository))?"


def _url(target: str) -> str | None:
    target = target.strip().rstrip("/") if target.strip().endswith("/") and "://" not in target else target.strip()
    m = re.fullmatch(r"(?:localhost|127\.0\.0\.1)(?:[\s:]+(\d{2,5}))?(/\S*)?", target, re.I)
    if m:
        return f"http://localhost{':' + m.group(1) if m.group(1) else ''}{m.group(2) or ''}"
    m = re.fullmatch(r"port\s+(\d{2,5})", target, re.I)
    if m:
        return f"http://localhost:{m.group(1)}"
    if re.match(r"^https?://\S+$", target, re.I):
        return target
    if re.fullmatch(r"[\w-]+(?:\.[\w-]+)+(?:/\S*)?", target) and not looks_like_file(target):
        return f"https://{target}"
    return None


KNOWN_APPS = (
    "slack",
    "terminal",
    "iterm",
    "iterm2",
    "vs code",
    "vscode",
    "visual studio code",
    "code",
    "xcode",
    "finder",
    "calculator",
    "notes",
    "spotify",
    "docker",
    "docker desktop",
    "postman",
    "figma",
    "zoom",
    "teams",
    "microsoft teams",
    "discord",
    "mail",
    "calendar",
    "music",
    "preview",
    "textedit",
    "system settings",
    "activity monitor",
    "intellij idea",
    "pycharm",
    "webstorm",
    "android studio",
    "sublime text",
    "notion",
)
ARTICLES = ("the", "a", "an", "my", "our", "this", "that", "some")


def _known_app(name: str) -> bool:
    """A known application: the fixed list, or an installed macOS app bundle with that exact name."""
    if name.lower() in KNOWN_APPS:
        return True
    import sys

    return sys.platform == "darwin" and any(
        (Path(base) / f"{name}.app").exists() for base in ("/Applications", "/System/Applications")
    )


def _open(m: MatchLike, ctx: ResolverContext) -> Step | None:
    target = m.group("target").strip()
    url = _url(target)
    if url is not None:
        return Step("browser.open", {"url": url}, f"open {url}")
    word = target.lower()
    if word in BROWSERS:
        return Step("browser.open", {"url": "about:blank"}, "open the browser")
    words = word.split()
    if looks_like_file(target) or "/" in target or not words or len(words) > 3 or words[0] in ARTICLES:
        return None
    if "and" in words or word in ("tests", "dev server", "services", "project"):
        return None
    explicit = m.group(0).lower().startswith("launch")
    if not explicit and not _known_app(target):
        return None  # "open <something>" is only deterministic for a known application
    return Step("computer.launch", {"name": target}, f"launch {target}")


def _search_web(m: MatchLike, _ctx: ResolverContext) -> Step | None:
    query = m.group("q").strip().strip("\"'")
    return Step("browser.open", {"url": DEFAULT_SEARCH_URL.format(query=quote_plus(query))}, f"search for {query!r}")


def _service_step(action: str, verb: str) -> Builder:
    def build(m: MatchLike, ctx: ResolverContext) -> Step | None:
        word = (m.groupdict().get("svc") or "").strip()
        if not word or word.lower() in ("services", "all services", "everything", "all"):
            return Step(action, {}, f"{verb} all services")
        names = ctx.service(word)
        if names is None:
            if word.lower() in ("server", "dev server", "development server") and action == "service.start":
                return Step("project.run", {}, "start the development server (highhx dev)")
            return None
        return Step(action, {"services": names} if names else {}, f"{verb} {', '.join(names) or 'all services'}")

    return build


def _logs(m: MatchLike, ctx: ResolverContext) -> Step | None:
    word = (m.groupdict().get("svc") or "").strip()
    if not word:
        return Step("service.logs", {}, "show the latest logs")
    names = ctx.service(word)
    if not names or len(names) != 1:
        return None
    return Step("service.logs", {"service": names[0]}, f"show {names[0]} logs")


def _deploy(action: str, verb: str, *, needs_target: bool = False) -> Builder:
    def build(m: MatchLike, ctx: ResolverContext) -> Step | None:
        word = (m.groupdict().get("env") or "").strip()
        if not word:
            if needs_target:
                return None  # changing a deployment needs a named target to be a complete request
            return Step(action, {}, f"{verb} (all targets)")
        env = ctx.environment(word)
        if env is None:
            return None
        return Step(action, {"environment": env}, f"{verb} {env}")

    return build


def _workflow_run(m: MatchLike, ctx: ResolverContext) -> Step | None:
    name = ctx.workflow(m.group("wf"))
    return Step("workflow.run", {"name": name}, f"run workflow {name}") if name else None


def _execution(action: str, verb: str) -> Builder:
    return lambda m, _c: Step(action, {"execution_id": m.group("id")}, f"{verb} workflow run {m.group('id')}")


def _read_file(m: MatchLike, ctx: ResolverContext) -> Step | None:
    path = ctx.existing_file(m.group("path"))
    return Step("filesystem.read", {"path": path}, f"read {path}") if path else None


def _search_code(m: MatchLike, _ctx: ResolverContext) -> Step | None:
    pattern = m.group("pat").strip()
    quoted = re.fullmatch(r"[\"'`](.+)[\"'`]", pattern)
    literal = quoted.group(1) if quoted else pattern
    if not literal or len(literal.split()) > 6:
        return None
    return Step("filesystem.search", {"pattern": re.escape(literal)}, f"search the code for {literal!r}")


def _commit(m: MatchLike, _ctx: ResolverContext) -> Step:
    message = m.group("msg")
    inputs: dict[str, Any] = {"message": message}
    if m.groupdict().get("all"):
        inputs["all"] = True
    return Step("git.commit", inputs, f"commit {message!r}")


def _branch(m: MatchLike, _ctx: ResolverContext) -> Step:
    return Step("git.branch", {"name": m.group("name")}, f"create branch {m.group('name')}")


def _checkout(m: MatchLike, _ctx: ResolverContext) -> Step:
    return Step("git.checkout", {"ref": m.group("ref")}, f"switch to {m.group('ref')}")


def _tag(m: MatchLike, _ctx: ResolverContext) -> Step:
    return Step("git.tag", {"name": m.group("name")}, f"tag {m.group('name')}")


def _click(m: MatchLike, _ctx: ResolverContext) -> Step:
    role = (m.groupdict().get("role") or "button").lower()
    name = m.group("name").strip().strip("\"'")
    return Step("browser.click", {"target": f"{role}:{name}"}, f"click {role} {name!r}")


def _db_restore(m: MatchLike, _ctx: ResolverContext) -> Step:
    backup = m.groupdict().get("backup")
    return Step("database.restore", {"backup": backup} if backup else {}, "restore the database")


ENV = r"(?P<env>[\w.-]+)"
SVC = r"(?P<svc>[\w.-]+(?:\s+server)?)"
QUOTED = r"[\"'](?P<msg>[^\"']+)[\"']"

RULES: tuple[Rule, ...] = (
    # tests / checks / build
    _rule(
        "test",
        rf"{RUN}{THE}(?:all\s+(?:the\s+)?|unit\s+)?tests?(?:\s+suite)?{PROJECT}|test{PROJECT}",
        _fixed("project.test", "run the tests"),
    ),
    _rule(
        "check",
        rf"{RUN}{THE}(?:checks?|lint(?:ing|er|ers)?|linters?|quality\s+checks?|type[- ]?checks?)",
        _fixed("project.check", "run the checks"),
    ),
    _rule(
        "fix",
        rf"{RUN}{THE}(?:auto(?:matic)?[- ]?fix(?:es|ers?)?|formatters?)|format\s+{THE}code|fix\s+{THE}(?:lint(?:ing)?|formatting|style)(?:\s+(?:issues|errors|problems))?",
        _fixed("project.fix", "apply formatter and linter fixes"),
    ),
    _rule("build", rf"{RUN}build{PROJECT}|build\s+it|compile{PROJECT}", _fixed("project.build", "build the project")),
    # project
    _rule("status", rf"{SHOW}{THE}(?:project\s+)?status|dashboard", _fixed("project.status", "project status")),
    _rule(
        "detect",
        rf"{SHOW}{THE}(?:project\s+)?(?:info(?:rmation)?|details|stack|context)|detect{PROJECT}",
        _fixed("project.detect", "detect the project"),
    ),
    _rule(
        "init",
        rf"(?:init(?:ialize|ialise)?|set\s*up|setup)\s+(?:highhx|{THE}project)(?:\s+for\s+highhx)?|highhx\s+init",
        _fixed("project.init", "initialize HighhX"),
    ),
    _rule(
        "dev",
        rf"{RUN}{THE}dev(?:elopment)?(?:\s+(?:server|environment|mode))?",
        _fixed("project.run", "start the development server (highhx dev)"),
    ),
    # services
    _rule(
        "services-start",
        rf"(?:start|launch|bring\s+up|spin\s+up)\s+{THE}(?:{SVC}|services|everything)",
        _service_step("service.start", "start"),
    ),
    _rule(
        "services-stop",
        rf"(?:stop|shut\s*down|kill|bring\s+down)\s+{THE}(?:{SVC}|services|everything)",
        _service_step("service.stop", "stop"),
    ),
    _rule(
        "services-restart",
        rf"(?:restart|reboot|bounce)\s+{THE}(?:{SVC}|services|everything)",
        _service_step("service.restart", "restart"),
    ),
    _rule(
        "logs",
        rf"{SHOW}{THE}(?:(?:recent|latest|last)\s+)?logs?(?:\s+(?:for|of|from)\s+{THE}{SVC})?",
        _logs,
    ),
    _rule("service-logs", rf"{SHOW}{THE}{SVC}\s+logs?", _logs),
    # packages
    _rule(
        "deps-install",
        rf"(?:install|set\s+up)\s+{THE}(?:dependencies|deps|packages|requirements)",
        _fixed("package.install", "install dependencies"),
    ),
    _rule(
        "deps-update",
        rf"(?:update|upgrade)\s+{THE}(?:dependencies|deps|packages)",
        _fixed("package.update", "update dependencies"),
    ),
    _rule(
        "deps-audit",
        rf"(?:audit\s+{THE}(?:dependencies|deps|packages)|{SHOW}{THE}vulnerable\s+(?:dependencies|packages))",
        _fixed("package.audit", "audit dependencies"),
    ),
    _rule(
        "deps-outdated",
        rf"{SHOW}{THE}outdated(?:\s+(?:dependencies|deps|packages))?",
        _fixed("package.outdated", "list outdated dependencies"),
    ),
    # security & diagnostics
    _rule(
        "security",
        rf"{RUN}{THE}security(?:\s+(?:scan|check|audit))?|(?:check|scan)\s+{THE}(?:security|for\s+(?:secrets|vulnerabilities|security\s+issues))|scan\s+for\s+secrets",
        _fixed("security.scan", "security scan"),
    ),
    _rule(
        "doctor",
        rf"{RUN}{THE}doctor|check\s+{THE}(?:setup|environment|toolchain)",
        _fixed("security.doctor", "check the setup (doctor)"),
    ),
    _rule(
        "diagnose",
        rf"{RUN}{THE}diagnos(?:e|is|tics)(?:\s+{THE}(?:project|setup))?|diagnose{PROJECT}",
        _fixed("security.diagnose", "diagnose the project"),
    ),
    # git
    _rule("git-status", rf"{SHOW}{THE}git\s+status|git\s+status", _fixed("git.status", "git status")),
    _rule(
        "git-diff",
        rf"{SHOW}{THE}(?:git\s+)?(?:diff|changes|uncommitted\s+changes)|what\s+(?:has\s+)?changed|git\s+diff",
        _fixed("git.diff", "show the diff"),
    ),
    _rule(
        "git-log",
        rf"{SHOW}{THE}(?:recent\s+|last\s+)?(?:commits|commit\s+history|git\s+(?:log|history))|git\s+log",
        _fixed("git.log", "recent commits"),
    ),
    _rule("git-branches", rf"{SHOW}{THE}(?:git\s+)?branches", _fixed("git.branch", "list branches")),
    _rule(
        "git-branch-create",
        r"(?:create|make|new)\s+(?:a\s+)?(?:new\s+)?branch\s+(?:called\s+|named\s+)?(?P<name>[\w./-]+)",
        _branch,
    ),
    _rule(
        "git-checkout",
        r"(?:switch\s+to|checkout|check\s+out)\s+(?:the\s+)?(?:branch\s+)?(?P<ref>[\w./-]+)(?:\s+branch)?",
        _checkout,
    ),
    _rule(
        "git-commit",
        rf"commit(?P<all>\s+(?:all|everything))?(?:\s+(?:my\s+|the\s+)?changes)?\s+(?:with\s+(?:the\s+)?message\s+|as\s+|-m\s+)?{QUOTED}",
        _commit,
    ),
    _rule(
        "git-push",
        r"(?:git\s+)?push(?:\s+(?:my\s+|the\s+)?(?:changes|commits))?(?:\s+to\s+(?:origin|the\s+remote|remote))?",
        _fixed("git.push", "push to origin"),
    ),
    _rule(
        "git-pull",
        r"(?:git\s+)?pull(?:\s+(?:the\s+)?latest(?:\s+changes)?)?(?:\s+from\s+(?:origin|the\s+remote|remote))?",
        _fixed("git.pull", "pull from origin"),
    ),
    _rule(
        "git-tag",
        r"(?:create\s+(?:a\s+)?)?tag(?:\s+(?:the\s+)?(?:release|version))?\s+(?:as\s+)?(?P<name>v?\d[\w.-]*)",
        _tag,
    ),
    # docker
    _rule(
        "docker-build",
        rf"(?:docker\s+build|build\s+{THE}(?:docker\s+)?(?:images?|containers?))",
        _fixed("docker.build", "build images"),
    ),
    _rule(
        "docker-up",
        rf"(?:docker\s+up|(?:start|run)\s+{THE}(?:docker\s+)?containers?|docker\s+compose\s+up)",
        _fixed("docker.run", "start containers"),
    ),
    _rule(
        "docker-down",
        rf"(?:docker\s+down|stop\s+{THE}(?:docker\s+)?containers?|docker\s+compose\s+down)",
        _fixed("docker.stop", "stop containers"),
    ),
    _rule(
        "docker-logs", rf"{SHOW}{THE}(?:docker|container)\s+logs|docker\s+logs", _fixed("docker.logs", "container logs")
    ),
    # database
    _rule(
        "db-status",
        rf"{SHOW}{THE}(?:database|db)\s+(?:status|connection)|(?:connect\s+to|test)\s+{THE}(?:database|db)(?:\s+connection)?",
        _fixed("database.connect", "database status"),
    ),
    _rule(
        "db-migrate",
        rf"(?:migrate\s+{THE}(?:database|db)|{RUN}{THE}(?:database\s+|db\s+)?migrations?)",
        _fixed("database.migrate", "migrate the database"),
    ),
    _rule(
        "db-backup", rf"(?:back\s*up|backup)\s+{THE}(?:database|db)", _fixed("database.backup", "back up the database")
    ),
    _rule("db-restore", rf"restore\s+{THE}(?:database|db)(?:\s+from\s+(?P<backup>[\w./-]+))?", _db_restore),
    # deployment
    _rule(
        "deploy",
        rf"(?:deploy|ship)(?:\s+{THE}(?:app|application|project|it))?(?:\s+(?:to\s+)?{THE}{ENV}(?:\s+environment)?)?",
        _deploy("deployment.deploy", "deploy to", needs_target=True),
    ),
    _rule(
        "rollback",
        rf"(?:roll\s*back|revert\s+{THE}deploy(?:ment)?)(?:\s+(?:the\s+)?(?:deploy(?:ment)?\s+)?(?:on\s+|to\s+|of\s+)?{THE}{ENV})?",
        _deploy("deployment.rollback", "roll back", needs_target=True),
    ),
    _rule(
        "deploy-status",
        rf"{SHOW}{THE}deploy(?:ment)?\s+status(?:\s+(?:of|for|on)\s+{THE}{ENV})?",
        _deploy("deployment.status", "deployment status"),
    ),
    _rule(
        "deploy-logs",
        rf"{SHOW}{THE}deploy(?:ment)?\s+logs(?:\s+(?:of|for|on)\s+{THE}{ENV})?",
        _deploy("deployment.logs", "deployment logs"),
    ),
    # workflows
    _rule(
        "workflow-run",
        r"(?:run|start|execute|trigger)\s+(?:the\s+)?(?P<wf>[\w.-]+)\s+workflow|(?:run|start|execute|trigger)\s+(?:the\s+)?workflow\s+(?P<wf2>[\w.-]+)",
        lambda m, c: _workflow_run(_Alt(m, "wf", "wf2"), c),
    ),
    _rule(
        "workflow-resume",
        r"resume\s+(?:the\s+)?(?:workflow\s+)?(?:run\s+)?(?P<id>[0-9a-f]{6,})",
        _execution("workflow.resume", "resume"),
    ),
    _rule(
        "workflow-cancel",
        r"(?:cancel|abort|stop)\s+(?:the\s+)?workflow\s+(?:run\s+)?(?P<id>[0-9a-f]{6,})",
        _execution("workflow.cancel", "cancel"),
    ),
    # files
    _rule(
        "read-file",
        r"(?:read|show|open|cat|view|print)\s+(?:me\s+)?(?:the\s+)?(?:file\s+)?(?P<path>[\w./-]+\.[\w]+|[\w./-]*/[\w./-]+)",
        _read_file,
    ),
    _rule(
        "search-code",
        r"(?:search|grep|find)\s+(?:the\s+)?(?:code(?:base)?\s+|files?\s+|project\s+)?(?:for\s+)?(?P<pat>[\"'`].+[\"'`])|(?:search|grep)\s+(?:for\s+)?(?P<pat2>\w+)(?:\s+in\s+(?:the\s+)?(?:code(?:base)?|files|project))?",
        lambda m, c: _search_code(_Alt(m, "pat", "pat2"), c),
    ),
    # browser & desktop
    _rule("search-web", r"(?:search(?:\s+the\s+web)?|google|look\s+up)(?:\s+for)?\s+(?P<q>.+)", _search_web),
    _rule(
        "screenshot",
        r"(?:take\s+(?:a\s+)?)?screenshot(?:\s+(?:of\s+)?(?:the\s+)?(?:page|browser))?",
        _fixed("browser.screenshot", "take a screenshot"),
    ),
    _rule(
        "read-page",
        r"(?:read|extract|get)\s+(?:the\s+)?(?:page|web\s*page)(?:\s+(?:text|content|data))?",
        _fixed("browser.extract", "read the page"),
    ),
    _rule(
        "click",
        r"click\s+(?:on\s+)?(?:the\s+)?[\"']?(?P<name>[\w .'-]+?)[\"']?(?:\s+(?P<role>button|link|tab|checkbox|menuitem))?",
        _click,
    ),
    _rule(
        "open",
        r"(?:open|go\s+to|navigate\s+to|visit|browse\s+to|launch)\s+(?P<target>.+?)(?:\s+in\s+(?:the\s+)?(?:browser|chrome))?",
        _open,
    ),
)


class _Alt:
    """A match proxy that reads ``primary`` or, when empty, ``fallback`` as ``primary``."""

    def __init__(self, match: MatchLike, primary: str, fallback: str) -> None:
        self._match, self._primary, self._fallback = match, primary, fallback

    def group(self, name: str | int, /) -> Any:
        if name == self._primary:
            return self._match.group(self._primary) or self._match.group(self._fallback) or ""
        return self._match.group(name)

    def groupdict(self) -> dict[str, Any]:
        return self._match.groupdict()


# ---------------------------------------------------------------- resolution
POLITE = re.compile(
    r"^(?:please\s+|pls\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|hey\s+|ok\s+|highhx,?\s+|i\s+want\s+to\s+|let'?s\s+|go\s+ahead\s+and\s+)+",
    re.I,
)
SPLIT = re.compile(r"\s*(?:,\s*then\s+|;\s*|\s+and\s+then\s+|\s+then\s+|\s+and\s+|,\s*and\s+|&&)\s*", re.I)


def normalise(text: str) -> str:
    text = " ".join(text.strip().split())
    text = POLITE.sub("", text)
    return text.rstrip(".!?").strip()


def _resolve_one(text: str, ctx: ResolverContext) -> tuple[Step, str] | None:
    for rule in RULES:
        match = rule.pattern.fullmatch(text)
        if match is None:
            continue
        step = rule.build(match, ctx)
        if step is not None:
            return step, rule.name
    return None


def resolve(text: str, context: ResolverContext | None = None) -> Resolution | None:
    """Resolve ``text`` to actions, or None when it is not a known, fully specified request."""
    ctx = context or ResolverContext()
    clean = normalise(text)
    if not clean:
        return None
    whole = _resolve_one(clean, ctx)
    if whole is not None:
        return Resolution((whole[0],), clean, whole[1])
    parts = [p for p in SPLIT.split(clean) if p]
    if len(parts) < 2 or len(parts) > 6:
        return None
    steps: list[Step] = []
    rules: list[str] = []
    for part in parts:
        found = _resolve_one(normalise(part), ctx)
        if found is None:
            return None  # all or nothing: never run half of a request
        steps.append(found[0])
        rules.append(found[1])
    return Resolution(tuple(steps), clean, "+".join(rules))
