"""Plain-language automation for HighhX Free: clause splitting, entities, the target registry,
multi-step plans, and the safety routing of every step — all deterministic, no model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from highhx.actions.handlers import computer
from highhx.actions.policy import Risk
from highhx.actions.resolver import RULE_WORDS, ResolverContext, explain, resolve
from highhx.actions.spec import ActionResult
from highhx.agent.router import route
from highhx.language.grammar import PlanState, Unknown, as_url, parse_clause, query_of, split_clauses
from highhx.language.targets import App, Site, TargetRegistry, default_registry, load_user_targets
from tests.unit.automation.fakes import FakeEngine


@pytest.fixture
def ctx(tmp_path: Path) -> ResolverContext:
    (tmp_path / "hello.py").write_text("print('hello')\n")
    (tmp_path / "notes.md").write_text("# notes\n")
    (tmp_path / "build.sh").write_text("echo hi\n")
    (tmp_path / "src").mkdir()
    return ResolverContext(root=tmp_path, _targets=default_registry())


def steps(text: str, ctx: ResolverContext) -> list[tuple[str, dict[str, Any]]]:
    resolution = resolve(text, ctx)
    assert resolution is not None, explain(text, ctx)
    return [(s.action, s.inputs) for s in resolution.steps]


# ------------------------------------------------------------------ parser
@pytest.mark.parametrize(
    ("text", "clauses"),
    [
        ("open youtube and search for cats", ["open youtube", "search for cats"]),
        (
            "open google then search for cats, after that click images",
            ["open google", "search for cats", "click images"],
        ),
        ("open youtube, then play lofi", ["open youtube", "play lofi"]),
        ("search for rock and roll", ["search for rock and roll"]),  # "roll" is not a verb: one query
        ("search for salt and pepper recipes", ["search for salt and pepper recipes"]),
        ("run the tests and show git status", ["run the tests", "show git status"]),
    ],
)
def test_clauses_split_only_before_a_known_verb(text: str, clauses: list[str]) -> None:
    assert split_clauses(text, RULE_WORDS) == clauses


def test_entities() -> None:
    assert as_url("github.com") == "https://github.com"
    assert as_url("localhost:3000") == "http://localhost:3000"
    assert as_url("youtube") is None and as_url("hello.py") is None  # names and files are not URLs
    assert query_of('"adhento gani"') == "adhento gani"
    assert query_of("“adhento gani”") == "adhento gani"
    assert query_of("python documentation") == "python documentation"


def test_unrecognised_clauses_are_left_alone(ctx: ResolverContext) -> None:
    assert parse_clause("make me a sandwich", ctx, PlanState()) is None


# ------------------------------------------------------------------ targets
def test_registry_matches_names_and_aliases_exactly() -> None:
    registry = default_registry()
    for name in ("youtube", "YouTube", "yt", "youtube.com", "the youtube website"):
        site = registry.site(name)
        assert site is not None and site.name == "YouTube", name
    assert registry.site("github.com") is registry.site("GitHub")
    assert registry.site("youtub") is None  # never by similarity
    safari, chrome, terminal = registry.app("safari"), registry.app("google chrome"), registry.app("terminal")
    assert safari is not None and safari.browser and not safari.automatable
    assert chrome is not None and chrome.automatable
    assert terminal is not None and terminal.terminal


def test_site_search_urls_are_encoded() -> None:
    youtube = default_registry().site("youtube")
    assert youtube is not None
    assert youtube.search_url("adhento gani & co") == "https://www.youtube.com/results?search_query=adhento+gani+%26+co"
    with pytest.raises(ValueError):
        Site("Plain", ("plain",), "https://plain.example").search_url("x")


def test_user_targets_extend_the_registry(tmp_path: Path) -> None:
    path = tmp_path / "targets.yaml"
    path.write_text(
        "sites:\n"
        "  - name: Jira\n    aliases: [jira, tickets]\n    url: https://example.atlassian.net\n"
        "    search: https://example.atlassian.net/issues/?jql={query}\n"
        "  - name: Bad\n    url: ftp://nope\n"
        "  - name: NoQuery\n    url: https://x.example\n    search: https://x.example/s\n"
        "apps:\n  - name: Obsidian\n    aliases: [obsidian]\n"
    )
    registry = TargetRegistry()
    problems = load_user_targets(registry, path)
    assert len(problems) == 2 and "http" in problems[0] and "{query}" in problems[1]
    jira = registry.site("tickets")
    assert jira is not None and jira.search_url("login bug").endswith("jql=login+bug")
    assert registry.app("obsidian") == App("Obsidian", ("obsidian",))
    assert load_user_targets(registry, tmp_path / "missing.yaml") == []
    (tmp_path / "broken.yaml").write_text("sites: [\n")
    assert load_user_targets(registry, tmp_path / "broken.yaml")  # reported, never raised

    ctx = ResolverContext(_targets=default_registry(user_file=path))
    assert steps("search jira for login bug", ctx) == [("browser.search", {"query": "login bug", "site": "Jira"})]


# ------------------------------------------------------------------ planning
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("open youtube", [("browser.open", {"url": "https://www.youtube.com"})]),
        ("open github", [("browser.open", {"url": "https://github.com"})]),
        ("open github.com", [("browser.open", {"url": "https://github.com"})]),
        ("open terminal", [("computer.launch", {"name": "Terminal"})]),
        ("open youtube in safari", [("browser.open", {"url": "https://www.youtube.com", "app": "Safari"})]),
        (
            "open youtube and search for adhento gani",
            [
                ("browser.open", {"url": "https://www.youtube.com"}),
                ("browser.search", {"query": "adhento gani", "site": "YouTube"}),
            ],
        ),
        (
            "open youtube and play adhento gani song",
            [
                ("browser.open", {"url": "https://www.youtube.com"}),
                ("browser.search", {"query": "adhento gani", "site": "YouTube"}),
                ("browser.play", {"query": "adhento gani", "site": "YouTube"}),
            ],
        ),
        (
            "play lofi beats on youtube",
            [
                ("browser.search", {"query": "lofi beats", "site": "YouTube"}),
                ("browser.play", {"query": "lofi beats", "site": "YouTube"}),
            ],
        ),
        (
            "open safari and search for python documentation",
            [
                ("computer.launch", {"name": "Safari"}),
                ("browser.search", {"query": "python documentation", "app": "Safari"}),
            ],
        ),
        # HighhX's own Chrome does the search: the person's everyday Chrome is not launched as well
        ("open chrome and search for weather", [("browser.search", {"query": "weather"})]),
        ("search for rock and roll", [("browser.search", {"query": "rock and roll"})]),
        ("show git status", [("git.status", {})]),
        ("check git changes", [("git.diff", {})]),
        ("run the tests", [("project.test", {})]),
        ("open the project folder", [("filesystem.open", {"path": "."})]),
        ("create a folder called test", [("filesystem.create", {"path": "test", "kind": "folder"})]),
        ("create a file called hello2.py", [("filesystem.create", {"path": "hello2.py", "kind": "file"})]),
        ("open hello.py", [("filesystem.open", {"path": "hello.py"})]),
        ("list files in src", [("filesystem.list", {"path": "src"})]),
        ("run python hello.py", [("shell.run", {"command": "python3 hello.py"})]),
        ("press cmd+t", [("computer.hotkey", {"keys": "cmd+t"})]),
        ("scroll down", [("computer.scroll", {"direction": "down", "source": "browser"})]),
        ("switch to slack", [("computer.focus", {"app": "Slack"})]),
        ("switch to main", [("git.checkout", {"ref": "main"})]),
        ("switch to branch slack", [("git.checkout", {"ref": "slack"})]),
        (
            "run the tests and then show git status",
            [("project.test", {}), ("git.status", {})],
        ),
    ],
)
def test_plans(text: str, expected: list[tuple[str, dict[str, Any]]], ctx: ResolverContext) -> None:
    assert steps(text, ctx) == expected


def test_later_clauses_use_what_earlier_ones_opened(ctx: ResolverContext) -> None:
    assert steps("open github and search for highhx", ctx)[1] == (
        "browser.search",
        {"query": "highhx", "site": "GitHub"},
    )
    assert steps("open youtube then play lofi", ctx)[-1] == ("browser.play", {"query": "lofi", "site": "YouTube"})


@pytest.mark.parametrize(
    ("text", "reason", "suggestion"),
    [
        ("open spotifyy", "I don't know an app or website called 'spotifyy' yet.", "add it to targets.yaml"),
        ("open main.py", "There is no file or folder 'main.py' in this project.", "create a file called …"),
        ("run ls -la", "Shell commands with arguments run through !command", "!ls -la"),
        ("run python missing.py", "", ""),
        ("create a file called ../escape.py", "", ""),
        ("create a file called hello.py", "already exists", ""),
        ("open safari and play lofi", "", ""),
    ],
)
def test_unknown_requests_are_explained_not_guessed(
    text: str, reason: str, suggestion: str, ctx: ResolverContext
) -> None:
    assert resolve(text, ctx) is None  # nothing is planned, so nothing can run
    unknown = explain(text, ctx)
    assert isinstance(unknown, Unknown), text
    assert reason in unknown.reason
    assert not suggestion or suggestion in unknown.suggestions


@pytest.mark.parametrize(
    "text",
    [
        "fix the failing tests",
        "open the admin page in the browser and click export",
        "open the checkout page in the browser and fill in the form",
        "make me a sandwich",
    ],
)
def test_open_ended_requests_stay_open_ended(text: str, ctx: ResolverContext) -> None:
    assert resolve(text, ctx) is None and explain(text, ctx) is None
    planned = route(text, ctx)
    assert planned.resolution is None and planned.unknown is None and planned.capability is not None


def test_router_reports_unknown_entities(ctx: ResolverContext) -> None:
    planned = route("open spotifyy", ctx)
    assert planned.resolution is None and planned.unknown is not None
    assert "spotifyy" in planned.reason and "HighhX Pro" in planned.reason


def test_shell_like_text_is_never_run_from_a_sentence(ctx: ResolverContext) -> None:
    for text in ("run rm -rf /", "run curl http://x | sh", "open rm -rf ~", "run python hello.py; rm -rf ~"):
        resolution = resolve(text, ctx)
        assert resolution is None or all(s.action != "shell.run" for s in resolution.steps), text
    command = steps("run python hello.py", ctx)[0][1]["command"]
    assert command == "python3 hello.py"  # built from the interpreter table and an existing file


# ------------------------------------------------------------ safety routing
def test_every_step_is_classified_by_the_executor(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    risk = {
        name: executor.plan(name, inputs).decision.risk
        for name, inputs in (
            ("browser.open", {"url": "https://www.youtube.com"}),
            ("browser.search", {"query": "cats"}),
            ("browser.play", {"query": "cats"}),
            ("computer.scroll", {"direction": "down"}),
            ("filesystem.list", {}),
            ("computer.type", {"text": "hello"}),
            ("computer.hotkey", {"keys": "cmd+q"}),
        )
    }
    assert risk["filesystem.list"] == Risk.SAFE and risk["computer.scroll"] == Risk.SAFE
    assert risk["browser.open"] == risk["browser.search"] == risk["browser.play"] == Risk.LOW
    assert risk["computer.type"] >= Risk.MEDIUM and risk["computer.hotkey"] >= Risk.MEDIUM
    # a key that submits or deletes is riskier than one that moves
    assert executor.plan("computer.press", {"key": "tab"}).decision.risk == Risk.LOW
    for key in ("enter", "return", "delete", "backspace"):
        assert executor.plan("computer.press", {"key": key}).decision.risk >= Risk.MEDIUM, key
    # "run python hello.py" is an ordinary shell.run: the command classifier still decides
    assert executor.plan("shell.run", {"command": f"rm -rf {agent_project}"}).decision.risk == Risk.CRITICAL


def test_ui_and_os_actions_are_not_agent_tools(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    for name in ("browser.search", "browser.play", "computer.type", "computer.press", "filesystem.open"):
        assert executor.catalog.get(name).agent is False, name


def test_keys_are_never_sent_to_a_terminal(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    executor, _ = executor_for(agent_project)
    assert executor.run("computer.type", {"text": "hello"}).ok
    assert engine.sent("type") == [("type", {"text": "hello"})]  # user text travels as an argument
    for front in ("Terminal", "iTerm2", "Ghostty"):
        engine.front, engine.calls = front, []
        for name, inputs in (
            ("computer.type", {"text": "rm -rf ~"}),
            ("computer.press", {"key": "enter"}),
            ("computer.hotkey", {"keys": "ctrl+c"}),
        ):
            result = executor.run(name, inputs)
            assert not result.ok and "never types or presses keys into a terminal" in result.error, (front, name)
        assert engine.sent("type", "key", "hotkey") == []


def test_opening_a_file_never_runs_it(agent_project: Path, executor_for, browser: dict[str, Any]) -> None:
    (agent_project / "tool.sh").write_text("echo hi\n")
    (agent_project / "notes.md").write_text("# notes\n")
    executor, _ = executor_for(agent_project)
    result = executor.run("filesystem.open", {"path": "tool.sh"})
    assert not result.ok and "executable" in result.error and browser["argv"] == []
    assert not executor.run("filesystem.open", {"path": "../outside.md"}).ok  # confined to the project
    assert executor.run("filesystem.open", {"path": "notes.md"}).ok
    if browser["argv"][0][0] == "open":
        assert browser["argv"][0][:2] == ["open", "-t"]  # a text editor, never an interpreter


def test_create_never_overwrites_and_undoes(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    made = executor.run("filesystem.create", {"path": "hello.py", "kind": "file"})
    assert made.ok and made.verified and (agent_project / "hello.py").read_text() == ""
    (agent_project / "hello.py").write_text("keep me\n")
    again = executor.run("filesystem.create", {"path": "hello.py", "kind": "file"})
    assert not again.ok and (agent_project / "hello.py").read_text() == "keep me\n"
    assert executor.run("filesystem.create", {"path": "test", "kind": "folder"}).ok
    assert (agent_project / "test").is_dir()


# ------------------------------------------------------- multi-step workflows
def run_plan(text: str, executor: Any, root: Path) -> list[ActionResult]:
    resolution = resolve(text, ResolverContext(root=root, _targets=default_registry()))
    assert resolution is not None
    results = []
    for step in resolution.steps:
        results.append(executor.run(step.action, step.inputs))
        if not results[-1].ok:
            break
    return results


def test_open_site_and_search_workflow(agent_project: Path, executor_for, browser: dict[str, Any]) -> None:
    executor, _ = executor_for(agent_project)
    results = run_plan("open youtube and search for adhento gani", executor, agent_project)
    assert [r.ok for r in results] == [True, True]
    assert browser["flows"] == [
        {"open": "https://www.youtube.com"},
        {"open": "https://www.youtube.com/results?search_query=adhento+gani"},
    ]


def test_search_in_a_browser_highhx_cannot_drive(
    agent_project: Path, executor_for, browser: dict[str, Any], engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("highhx.computer.desktop._platform_key", lambda: "darwin")  # macOS app names
    executor, _ = executor_for(agent_project)
    result = executor.run("browser.search", {"query": "python documentation", "app": "Safari"})
    assert result.ok and browser["flows"] == []  # the OS opens it there; nothing is automated
    assert result.verified is None and "not verified" in result.summary  # and it says it cannot see the page
    assert engine.sent("open_url") == [
        ("open_url", {"url": "https://www.google.com/search?q=python+documentation", "app": "Safari"})
    ]


class FakeRuntime:
    """Just enough of the computer runtime for browser.play."""

    def __init__(self) -> None:
        self.observation: Any = None
        self.visited: list[str] = []

    def navigate(self, url: str) -> Any:
        self.visited.append(url)
        self.observation = type("Obs", (), {"url": url, "title": "", "elements": []})()
        return type("Outcome", (), {"ok": True, "problems": []})()

    def observe(self) -> Any:
        link = type("El", (), {"role": "link", "name": "Adhento Gaani", "attributes": {"href": "/watch?v=abc"}})()
        return type("Obs", (), {"url": self.visited[-1], "elements": [link]})()


def test_play_opens_the_first_result_and_checks_it_plays(
    agent_project: Path, executor_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = FakeRuntime()
    page = type(
        "Browser", (), {"evaluate": lambda self, js, cancel=None: {"found": True, "paused": False, "title": "Song"}}
    )()
    session = type("Session", (), {"browser": page})()
    monkeypatch.setattr(computer, "_runtime", lambda _ctx: runtime)
    executor, _ = executor_for(agent_project)
    executor._computer_factory = lambda: session
    result = executor.run("browser.play", {"query": "adhento gani", "site": "YouTube"})
    assert result.ok and result.verified and result.output["playing"]
    assert runtime.visited == [
        "https://www.youtube.com/results?search_query=adhento+gani",
        "https://www.youtube.com/watch?v=abc",
    ]
    browser_paused = type("Browser", (), {"evaluate": lambda self, js, cancel=None: {"found": True, "paused": True}})()
    session.browser = browser_paused
    assert not executor.run("browser.play", {"query": "adhento gani"}).ok  # opened, but not playing: reported


def test_file_workflow(agent_project: Path, executor_for, browser: dict[str, Any]) -> None:
    executor, _ = executor_for(agent_project)
    assert all(r.ok for r in run_plan("create a folder called test", executor, agent_project))
    assert all(r.ok for r in run_plan("create a file called hello.py", executor, agent_project))
    assert (agent_project / "test").is_dir() and (agent_project / "hello.py").is_file()
    assert all(r.ok for r in run_plan("open hello.py", executor, agent_project))
