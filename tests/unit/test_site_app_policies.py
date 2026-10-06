"""Domain (host) and application (app) policy rules: enforced by the one policy engine for every
browser and desktop action — a navigation's destination, the page in front for clicks and typing,
the named or frontmost application for desktop input."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from highhx.actions.executor import ActionExecutor
from highhx.automation.engine.bridge import AutomationBridge
from highhx.benchmarks.environments.web import FakeWebApp
from highhx.computer.driver import HighhXDriver
from highhx.computer.session import ComputerSession
from highhx.policy.rules import host_matches
from highhx.safety.actions import ActionDescriptor, ActionKind, Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode, where
from tests.computer_use.environment import SimulatedDesktop
from tests.unit.agent.conftest import RecordingUI, agent_project, make_app  # noqa: F401

DESKTOP = Path(__file__).resolve().parents[1] / "computer_use" / "tasks" / "_desktop.yaml"
POLICIES = {
    "rules": [
        {
            "id": "no-bank",
            "effect": "deny",
            "when": {"action": "action:browser.*", "host": "bank.test"},
            "message": "no banking",
        },
        {"id": "ask-mail", "effect": "require_approval", "when": {"action": "action:browser.*", "host": "*.mail.test"}},
        {"id": "no-typing-notes", "effect": "deny", "when": {"action": "action:computer.type", "app": "notes"}},
    ]
}


def test_hosts_match_a_site_and_its_subdomains_never_a_substring() -> None:
    assert host_matches("bank.test", "bank.test") and host_matches("www.Bank.test.", "bank.test")
    assert not host_matches("notbank.test", "bank.test") and not host_matches("bank.test.evil.com", "bank.test")
    assert host_matches("a.mail.test", "*.mail.test") and not host_matches("mail.test", "*.mail.test")


@pytest.fixture
def setup(agent_project: Path, make_app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    (agent_project / ".highhx").mkdir(exist_ok=True)
    (agent_project / ".highhx" / "policies.yaml").write_text(yaml.safe_dump(POLICIES))
    web = FakeWebApp()
    env = SimulatedDesktop(yaml.safe_load(DESKTOP.read_text()))
    driver = HighhXDriver(AutomationBridge(env))
    monkeypatch.setattr(ComputerSession, "browser", property(lambda self: web))
    monkeypatch.setattr(ComputerSession, "driver", lambda self: driver)
    app = make_app(agent_project)
    ui = RecordingUI()
    gate = ActionGate(app.engine, ui, source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    session = ComputerSession(gate, actor=Actor.USER)
    session._browser = web  # the browser in use (the policy reads only one already in use)
    executor = ActionExecutor(app, gate, actor=Actor.USER, computer=lambda: session, sleep=lambda _s: None)
    return executor, web, env, ui


def test_a_site_rule_holds_for_navigation_and_for_acting_on_the_page(setup: Any) -> None:
    executor, web, _env, _ui = setup
    blocked = executor.run("browser.open", {"url": "https://www.bank.test/login"})
    assert blocked.status == "blocked" and "no banking" in str(blocked.error) + str(blocked.summary)
    assert executor.run("browser.open", {"url": "https://shop.test/invoices"}).ok
    # a link took the browser to the bank: a click there is held to the same rule
    web.url = "https://bank.test/transfer"
    clicked = executor.run("browser.click", {"target": "Export"})
    assert clicked.status == "blocked"
    web.url = "https://shop.test/invoices"
    assert executor.run("browser.click", {"target": "Export"}).status != "blocked"


def test_an_approval_rule_for_a_site_asks(setup: Any) -> None:
    executor, _web, _env, ui = setup
    ui.default_permission = "no"
    ui.default_action_answer = False
    result = executor.run("browser.open", {"url": "https://inbox.mail.test/"})
    assert result.status == "denied" and any(e[0] in ("permission", "confirm_action") for e in ui.events)


def test_an_application_rule_holds_for_the_frontmost_application(setup: Any) -> None:
    executor, _web, env, _ui = setup
    assert env.front == "Notes"
    assert executor.run("computer.type", {"text": "hello"}).status == "blocked"  # Notes is in front
    assert executor.run("computer.type", {"text": "hello", "app": "Finder"}).status != "blocked"


def test_a_runtime_description_names_its_site_and_application() -> None:
    action = ActionDescriptor(
        ActionKind.UI_CLICK, "Click", "computer", application="Chrome <https://www.bank.test/a?b=c>"
    )
    assert where(action) == ("www.bank.test", "Chrome")
    assert where(ActionDescriptor(ActionKind.READ, "r", "fs", application="project demo")) == (None, None)


def test_require_approval_rules_ask_for_any_action(agent_project: Path, make_app: Any) -> None:  # noqa: F811
    # Regression: the gate evaluated project policy but dropped a require_approval decision, so
    # such rules never asked — only deny rules had any effect on executor actions.
    rules = [
        {
            "id": "ask-read",
            "effect": "require_approval",
            "message": "reads are reviewed",
            "when": {"action": "action:filesystem.read"},
        },
        {
            "id": "ask-list",
            "effect": "require_approval",
            "bypassable": False,
            "when": {"action": "action:filesystem.list"},
        },
    ]
    (agent_project / ".highhx" / "policies.yaml").write_text(yaml.safe_dump({"rules": rules}))
    (agent_project / "a.txt").write_text("x")
    app = make_app(agent_project)
    ui = RecordingUI(default_permission="no", default_action_answer=False)
    gate = ActionGate(app.engine, ui, source="test", mode=ApprovalMode.ASK)
    executor = ActionExecutor(app, gate, actor=Actor.USER)
    refused = executor.run("filesystem.read", {"path": "a.txt"})
    assert (
        refused.status == "denied" and ui.requests and any("reads are reviewed" in r for r in ui.requests[-1].reasons)
    )
    ui.default_action_answer = True
    assert executor.run("filesystem.read", {"path": "a.txt"}).ok  # approved: it runs
    ui.default_action_answer, gate.assume_yes = False, True
    assert executor.run("filesystem.read", {"path": "a.txt"}).ok  # --yes answers a bypassable rule
    asked = len(ui.requests)
    assert executor.run("filesystem.list", {"path": "."}).status == "denied"  # but never a non-bypassable one
    assert len(ui.requests) == asked + 1
    executor.close()
