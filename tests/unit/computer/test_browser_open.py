"""``open <website>`` in HighhX Free, end to end, against a scripted Chrome.

    user text → deterministic resolver → JSON plan → action executor (risk, gate, audit)
              → ComputerRuntime → ChromeBrowser → (FakeChrome) → verification → run trace

Where "open X" goes is a decision about the working tab: a tab already showing X is used (never
reloaded, never duplicated); a blank tab or one on the same site is navigated; a tab showing
another site is kept and X opens in a new tab. The browser's failures (closed tab, lost
connection, exited browser) are recovered on the same path.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.resolver import ResolverContext, remote_web_url
from highhx.agent.router import route
from highhx.computer.browser import ChromeBrowser, _replaceable
from highhx.computer.session import ComputerSession
from highhx.language.targets import default_registry
from tests.unit.computer.test_browser_recovery import FakeChrome, _events, _sent


def _pages(chrome: FakeChrome) -> list[str]:
    return sorted(str(t["url"]) for t in chrome.tabs.values())


# ------------------------------------------------------------ which tab
def test_blank_start_opens_in_the_blank_tab(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    result = browser.navigate("https://www.youtube.com", reuse_tab=True)
    assert result.url == "https://www.youtube.com" and result.tab == "t1" and not result.new_tab
    assert list(chrome.tabs) == ["t1"]


def test_page_already_open_in_the_working_tab_is_not_reloaded(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://www.youtube.com", reuse_tab=True)
    navigations = len(_sent(chrome, "Page.navigate"))
    result = browser.navigate("https://youtube.com/", reuse_tab=True)  # the same page, spelled differently
    assert result.reused_tab and result.tab == "t1"
    assert len(_sent(chrome, "Page.navigate")) == navigations  # no reload: whatever is on the page stays
    assert "already_open" in _events(browser)


def test_page_open_in_another_tab_is_switched_to(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://www.youtube.com", reuse_tab=True)
    browser.navigate("https://github.com", reuse_tab=True)  # a new tab: YouTube stays
    result = browser.navigate("https://www.youtube.com", reuse_tab=True)
    assert result.reused_tab and result.tab == "t1" and browser._target_id == "t1"
    assert len(chrome.tabs) == 2


def test_another_site_opens_in_a_new_tab_and_keeps_the_page(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://mail.google.com/", reuse_tab=True)
    result = browser.navigate("https://github.com", reuse_tab=True)
    assert result.new_tab and result.tab != "t1" and browser._target_id == result.tab
    assert chrome.tabs["t1"]["url"] == "https://mail.google.com/"  # never navigated away
    assert "opened_beside" in _events(browser)


def test_the_same_site_is_navigated_in_place(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://github.com", reuse_tab=True)
    result = browser.navigate("https://github.com/me/app", reuse_tab=True)
    assert result.tab == "t1" and not result.new_tab and list(chrome.tabs) == ["t1"]


def test_repeated_opens_never_duplicate_tabs(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    for url in ["https://www.youtube.com", "https://github.com", "https://mail.google.com/"] * 3:
        assert browser.navigate(url, reuse_tab=True).url == url
    assert _pages(chrome) == ["https://github.com", "https://mail.google.com/", "https://www.youtube.com"]


def test_internal_navigation_still_uses_the_working_tab(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://www.youtube.com", reuse_tab=True)
    browser.navigate("https://www.youtube.com/results?search_query=lofi")  # a search: same tab
    assert list(chrome.tabs) == ["t1"]


@pytest.mark.parametrize(
    ("current", "requested", "replaceable"),
    [
        ("", "https://a.test/", True),
        ("about:blank", "https://a.test/", True),
        ("chrome://newtab/", "https://a.test/", True),
        ("https://www.github.com/x", "https://github.com/y", True),
        ("https://mail.google.com/mail/u/0/", "https://github.com", False),
        ("https://a.test/", "https://b.test/", False),
    ],
)
def test_replaceable(current: str, requested: str, replaceable: bool) -> None:
    assert _replaceable(current, requested) is replaceable


# ------------------------------------------------------------ recovery
def test_working_tab_closed_is_recreated(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://www.youtube.com", reuse_tab=True)
    chrome.close_tab("t1")
    result = browser.navigate("https://www.youtube.com", reuse_tab=True)
    assert result.url == "https://www.youtube.com" and result.tab != "t1" and result.tab in chrome.tabs
    assert len(chrome.tabs) == 1


def test_connection_lost_reconnects_and_reuses_the_open_page(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://www.youtube.com", reuse_tab=True)
    assert browser._conn is not None
    browser._conn.ws.close()
    navigations = len(_sent(chrome, "Page.navigate"))
    result = browser.navigate("https://www.youtube.com", reuse_tab=True)
    assert result.reused_tab and result.tab == "t1" and browser.reconnects == 1
    assert len(_sent(chrome, "Page.navigate")) == navigations


def test_connection_dropping_during_the_open_is_retried_safely(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.current_url()
    chrome.drop_after.append(("Page.navigate", True))  # it ran; the answer was lost
    result = browser.navigate("https://www.youtube.com", reuse_tab=True)
    assert result.url == "https://www.youtube.com" and browser.reconnects == 1
    assert len(_sent(chrome, "Page.navigate")) == 1  # it had arrived: not requested again


def test_browser_exited_is_restarted_and_the_page_opened(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://github.com", reuse_tab=True)
    chrome.kill()
    result = browser.navigate("https://www.youtube.com", reuse_tab=True)
    assert result.url == "https://www.youtube.com" and browser.started == [4243]  # type: ignore[attr-defined]


def test_browser_already_running_is_reused_not_started(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    chrome.tabs["t1"]["url"] = "https://www.youtube.com"
    result = browser.navigate("https://www.youtube.com", reuse_tab=True)
    assert result.reused_tab and browser.started == [] and chrome.connections == 1  # type: ignore[attr-defined]


# ------------------------------------------------------------ resolver
@pytest.mark.parametrize("site", sorted(default_registry().sites.values(), key=lambda s: s.id), ids=lambda s: s.id)
def test_every_registry_site_opens_locally(site: Any) -> None:
    for alias in site.aliases:
        planned = route(f"open {alias}")
        assert planned.resolution is not None, alias
        assert [(s.action, s.inputs) for s in planned.resolution.steps] == [("browser.open", {"url": site.url})]


@pytest.mark.parametrize(
    "text", ["open https://youtube.com", "open youtube.com", "go to https://example.org/docs", "visit example.org"]
)
def test_urls_open_locally(text: str) -> None:
    planned = route(text)
    assert planned.resolution is not None and planned.resolution.action == "browser.open"


@pytest.mark.parametrize(
    ("text", "actions"),
    [
        ("open youtube and search lofi music", ["browser.open", "browser.search"]),
        ("open gmail and search internship", ["browser.open", "browser.search"]),
        ("open youtube then play lofi", ["browser.open", "browser.search", "browser.play"]),
    ],
)
def test_multi_step_browser_requests_resolve_locally(text: str, actions: list[str]) -> None:
    planned = route(text)
    assert planned.resolution is not None and planned.capability is None
    assert [s.action for s in planned.resolution.steps] == actions


def test_open_ended_browser_requests_still_need_pro() -> None:
    planned = route("log in to my bank website and fill out the tax form for me")
    assert planned.resolution is None and planned.capability is not None


@pytest.mark.parametrize(
    ("remote", "web"),
    [
        ("https://github.com/me/app.git", "https://github.com/me/app"),
        ("https://user:token@github.com/me/app.git", "https://github.com/me/app"),
        ("git@github.com:me/app.git", "https://github.com/me/app"),
        ("ssh://git@gitlab.com/group/sub/app.git", "https://gitlab.com/group/sub/app"),
        ("/srv/git/app.git", None),
        ("file:///srv/git/app.git", None),
    ],
)
def test_remote_web_url(remote: str, web: str | None) -> None:
    assert remote_web_url(remote) == web


def _git_remote(root: Path, url: str) -> None:
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "remote", "add", "origin", url], cwd=root, check=True)


def test_my_repository_on_a_code_host_is_the_project_remote(tmp_path: Path) -> None:
    _git_remote(tmp_path, "git@github.com:me/app.git")
    planned = route("open github and open my repository", ResolverContext(root=tmp_path))
    assert planned.resolution is not None
    assert [s.inputs for s in planned.resolution.steps] == [
        {"url": "https://github.com"},
        {"url": "https://github.com/me/app"},
    ]


def test_my_repository_without_a_matching_remote_is_the_project_folder(tmp_path: Path) -> None:
    _git_remote(tmp_path, "https://gitlab.com/me/app.git")
    planned = route("open github and open my repository", ResolverContext(root=tmp_path))
    assert planned.resolution is not None
    assert planned.resolution.steps[1].action == "filesystem.open"


# ------------------------------------------------------------ end to end
@pytest.fixture
def live(browser: ChromeBrowser, monkeypatch: pytest.MonkeyPatch) -> ChromeBrowser:
    """The CLI's own computer session drives the scripted Chrome."""
    monkeypatch.setattr(ComputerSession, "browser", property(lambda self: browser))
    return browser


def _audit(root: Path) -> list[dict[str, Any]]:
    from highhx.commands import App
    from highhx.storage.database import Database
    from highhx.utils.paths import user_data_dir

    app = App(cwd=root)
    path = app.paths.db_file if app.initialized else user_data_dir() / "history.db"
    db = Database.open(path)
    try:
        rows = db.query("SELECT action, status, details FROM audit_log ORDER BY created_at, rowid")
    finally:
        db.close()
    return [{"action": r["action"], "status": r["status"], "details": json.loads(r["details"])} for r in rows]


@pytest.mark.parametrize(
    ("text", "url"),
    [
        ("open youtube", "https://www.youtube.com"),
        ("open gmail", "https://mail.google.com/"),
        ("open github", "https://github.com"),
        ("open wikipedia", "https://en.wikipedia.org"),
        ("open https://youtube.com", "https://youtube.com"),
    ],
)
def test_open_website_end_to_end(
    cli: Callable[..., Any], repo: Path, live: ChromeBrowser, chrome: FakeChrome, text: str, url: str
) -> None:
    result = cli(text, cwd=repo)
    out = result.stdout + result.stderr
    assert result.code == 0, out
    assert "Pro" not in out and "Nothing local maps" not in out
    assert "browser.open · low" in out and "↳ verified" in out and "is open" in out
    assert chrome.tabs["t1"]["url"] == url
    audit = _audit(repo)
    executed, navigated = audit[-1], audit[-2]  # the executor's record, around the browser gate's own
    assert executed["action"] == f"browser.open · {url}" and executed["status"] == "ok"
    assert navigated["action"] == f"Open {url}" and navigated["status"] == "ok"
    assert navigated["details"]["navigation"]["url"] == url


def test_open_youtube_and_search_end_to_end(
    cli: Callable[..., Any], repo: Path, live: ChromeBrowser, chrome: FakeChrome
) -> None:
    result = cli("open youtube and search lofi music", cwd=repo)
    assert result.code == 0, result.stdout + result.stderr
    assert "2/2 steps · verified" in result.stdout
    assert list(chrome.tabs) == ["t1"]
    assert chrome.tabs["t1"]["url"] == "https://www.youtube.com/results?search_query=lofi+music"


def test_open_github_and_my_repository_end_to_end(
    cli: Callable[..., Any], repo: Path, live: ChromeBrowser, chrome: FakeChrome
) -> None:
    _git_remote(repo, "https://github.com/me/app.git")
    result = cli("open github and open my repository", cwd=repo)
    assert result.code == 0, result.stdout + result.stderr
    assert "2/2 steps · verified" in result.stdout
    assert _pages(chrome) == ["https://github.com/me/app"]  # same site: one tab


def test_open_again_after_the_tab_was_closed_end_to_end(
    cli: Callable[..., Any], repo: Path, live: ChromeBrowser, chrome: FakeChrome
) -> None:
    assert cli("open youtube", cwd=repo).code == 0
    chrome.close_tab("t1")
    result = cli("open youtube", cwd=repo)
    assert result.code == 0, result.stdout
    assert _pages(chrome) == ["https://www.youtube.com"]


def test_open_a_second_site_keeps_the_first_end_to_end(
    cli: Callable[..., Any], repo: Path, live: ChromeBrowser, chrome: FakeChrome
) -> None:
    assert cli("open gmail", cwd=repo).code == 0
    result = cli("open youtube", cwd=repo)
    assert result.code == 0 and "in a new tab" in result.stdout
    assert _pages(chrome) == ["https://mail.google.com/", "https://www.youtube.com"]
    again = cli("open gmail", cwd=repo)
    assert again.code == 0 and "already open" in again.stdout and len(chrome.tabs) == 2


def test_dry_run_previews_without_touching_the_browser(
    cli: Callable[..., Any], repo: Path, live: ChromeBrowser, chrome: FakeChrome
) -> None:
    result = cli("--dry-run", "open youtube", cwd=repo)
    assert result.code == 0, result.stdout
    assert "verification failed" not in result.stdout and "dry run" in result.stdout
    assert chrome.sent == []
