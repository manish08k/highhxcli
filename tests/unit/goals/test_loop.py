"""The goal loop on websites HighhX was never configured for: generic primitives, verification,
recovery, loops and conditions, and the limits that keep it from spinning."""

from __future__ import annotations

import threading
from typing import Any

from highhx.core.errors import IntegrationError, OutcomeUnknownError
from highhx.goals.ir import TaskIR, parse_task
from highhx.goals.state import CANCELLED, COMPLETED, FAILED
from highhx.safety.actions import Actor
from tests.unit.agent.conftest import RecordingUI
from tests.unit.goals.conftest import Ctl, FakeWeb, Kit, Page, make_kit

BOOKS = "https://books.example/"


def act(action: str, **fields: Any) -> dict[str, Any]:
    return {"action": f"browser.{action}", "reason": f"test {action}", **fields}


def task(*steps: dict[str, Any], **fields: Any) -> TaskIR:
    constraints = {"action_timeout": 0.3, **fields.pop("constraints", {})}
    data: dict[str, Any] = {"goal": "a test goal", "constraints": constraints, "steps": list(steps), **fields}
    return parse_task(data)


# ----------------------------------------------------------- basic primitives
def test_generic_navigation_on_an_unknown_site(kit: Kit, web: FakeWeb) -> None:
    state = kit.run(task(act("navigate", value="books.example/", expect={"title_contains": "Books"})))
    assert state.status == COMPLETED and web.url == BOOKS
    assert state.steps[0].ok and state.steps[0].verified


def test_find_click_type_and_press_compose_a_search(kit: Kit, web: FakeWeb) -> None:
    state = kit.run(
        task(
            act("navigate", value=BOOKS),
            act("find", target="searchbox"),
            act("type", target="searchbox:Search books", value="dune"),
            act("press", value="enter", expect={"url_contains": "/search?q=dune"}),
            act("click", target="link#1", expect={"title_contains": "Book"}),
        )
    )
    assert state.status == COMPLETED, state.reason
    assert web.url == "https://books.example/book/1"
    assert [s.action.primitive for s in state.steps] == ["navigate", "find", "type", "press", "click"]
    assert all(s.verified is not False for s in state.steps)


def test_read_extracts_what_the_page_says(kit: Kit) -> None:
    state = kit.run(
        task(
            act("navigate", value=BOOKS),
            act("click", target="link:Contact"),
            act("read", value=r"[\w.+-]+@[\w-]+\.[\w.]+"),
        )
    )
    assert state.status == COMPLETED
    assert state.extracted["step_3"] == ["hello@books.example"]


def test_every_action_goes_through_the_gate_and_the_audit(kit: Kit) -> None:
    kit.run(task(act("navigate", value=BOOKS), act("click", target="link:Contact")))
    actions = [a["action"] for a in kit.audit()]
    assert actions[0] == f"Open {BOOKS}" and any("Contact" in a for a in actions[1:])


def test_a_denied_sensitive_action_ends_the_task_and_is_not_retried(app: Any) -> None:
    web = FakeWeb()
    web.pages[BOOKS].controls.append(
        Ctl("button", "Delete account", on_click=lambda w: None, attrs={"tag": "button", "class": "btn-danger"})
    )
    ui = RecordingUI(default_action_answer=False)
    kit = make_kit(app, web, ui=ui)
    state = kit.run(task(act("navigate", value=BOOKS), act("click", target="button:Delete account")))
    assert state.status == FAILED and "not allowed" in state.reason
    assert not any(c.startswith("click Delete") for c in web.calls)
    assert len([s for s in state.steps if s.action.primitive == "click"]) == 1


def test_the_agent_may_not_type_into_password_fields(app: Any) -> None:
    web = FakeWeb()
    web.pages[BOOKS].controls.append(Ctl("textbox", "Password", attrs={"tag": "input", "type": "password"}))
    kit = make_kit(app, web, actor=Actor.AGENT)
    state = kit.run(task(act("navigate", value=BOOKS), act("type", target="textbox:Password", value="hunter2")))
    assert state.status == FAILED
    assert not any(c.startswith("type Password") for c in web.calls)


# ----------------------------------------------------------- tabs
def test_new_tab_switch_back_and_close(kit: Kit, web: FakeWeb) -> None:
    state = kit.run(
        task(
            act("navigate", value=BOOKS),
            act("new_tab", value="https://tube.example/"),
            act("switch_tab", target="books.example"),
            act("click", target="link:Contact"),
            act("back", expect={"url_contains": "books.example/"}),
            act("switch_tab", target="tube.example"),
            act("close_tab"),
        )
    )
    assert state.status == COMPLETED, state.reason
    assert list(web.tabs) == ["t1"] and web.url == BOOKS


# ----------------------------------------------------------- verification
def test_an_expectation_that_never_holds_fails_the_step(kit: Kit) -> None:
    state = kit.run(task(act("navigate", value=BOOKS, expect={"text": "Page that is not there"})))
    assert state.status == FAILED
    assert state.steps[0].problems and "not on the page" in state.steps[0].problems[0]


def test_success_conditions_are_checked_not_assumed(kit: Kit) -> None:
    state = kit.run(task(act("navigate", value=BOOKS), success_conditions=[{"text": "hello@books.example"}]))
    assert state.status == FAILED and "goal is not reached" in state.reason
    assert any(e.kind == "VERIFY" and e.ok is False and "goal" in e.message for e in kit.log.entries)


def test_the_task_ends_as_soon_as_the_goal_holds(kit: Kit) -> None:
    state = kit.run(
        task(
            act("navigate", value=BOOKS),
            act("click", target="link:Contact"),
            act("click", target="link:Nowhere"),
            success_conditions=[{"url_contains": "/contact"}],
        )
    )
    assert state.status == COMPLETED and len(state.steps) == 2


def test_media_playing_is_verified_generically(kit: Kit, web: FakeWeb) -> None:
    state = kit.run(
        task(
            act("navigate", value="https://tube.example/"),
            act("type", target="searchbox", value="lofi"),
            act("click", target="button:Search", expect={"url_contains": "/search"}),
            act("click", target="link[href*=/watch]#1", expect={"url_contains": "/watch"}),
            act("verify", expect={"media_playing": True}),
            success_conditions=[{"media_playing": True}],
        )
    )
    assert state.status == COMPLETED, state.reason
    assert web.url == "https://tube.example/watch?v=1" and web.playing


def test_the_log_shows_every_phase(kit: Kit) -> None:
    kit.run(task(act("navigate", value=BOOKS), success_conditions=[{"title_contains": "Books"}]))
    kinds = kit.log.kinds()
    assert kinds[:3] == ["TASK", "PLAN", "PLAN"] and "OBSERVE" in kinds
    assert kinds.index("ACTION") < kinds.index("RESULT") < kinds.index("VERIFY") and kinds[-1] == "FINAL"
    assert kit.log.entries[-1].ok and "Task completed" in kit.log.text()


# ----------------------------------------------------------- recovery
def test_a_slow_element_is_waited_for(kit: Kit, web: FakeWeb) -> None:
    web.pages["https://books.example/contact"].controls.append(
        Ctl("button", "Chat", appear_after=3, on_click=lambda w: None)
    )
    state = kit.run(
        task(act("navigate", value="https://books.example/contact"), act("find", target="button:Chat", timeout=2))
    )
    assert state.status == COMPLETED and state.recoveries == 0


def test_an_element_below_the_fold_is_found_by_scrolling(kit: Kit, web: FakeWeb) -> None:
    state = kit.run(task(act("navigate", value=BOOKS), act("click", target="link:Weekly deals")))
    assert state.status == COMPLETED, state.reason
    assert "scroll down" in web.calls and web.url == "https://books.example/deals"
    assert any(e.kind == "RECOVERY" and "scrolling" in e.message for e in kit.log.entries)


def test_a_lost_connection_is_recovered_and_navigation_retried(kit: Kit, web: FakeWeb) -> None:
    web.fail["navigate"] = [IntegrationError("The browser connection was lost.")]
    state = kit.run(task(act("navigate", value=BOOKS)))
    assert state.status == COMPLETED and web.url == BOOKS
    assert [c for c in web.calls if c.startswith("navigate")] == [f"navigate {BOOKS}"] * 2
    assert state.steps[0].recovery.startswith("the browser recovered")


def test_a_click_with_an_unknown_outcome_is_never_repeated(kit: Kit, web: FakeWeb) -> None:
    web.fail["click"] = [OutcomeUnknownError("The answer was lost.")]
    state = kit.run(task(act("navigate", value=BOOKS), act("click", target="link:Contact")))
    assert state.status == FAILED
    assert [c for c in web.calls if c.startswith("click")] == ["click Contact us"]


def test_a_failed_click_is_not_blindly_retried(kit: Kit, web: FakeWeb) -> None:
    web.fail["click"] = [IntegrationError("The browser failed 3 times.")]
    state = kit.run(task(act("navigate", value=BOOKS), act("click", target="link:Contact")))
    assert state.status == FAILED
    assert len([c for c in web.calls if c.startswith("click")]) == 1


def test_navigation_to_a_site_that_does_not_resolve_fails_cleanly(kit: Kit) -> None:
    state = kit.run(task(act("navigate", value="https://no-such-site.invalid/")))
    assert state.status == FAILED
    assert "ERR_NAME_NOT_RESOLVED" in " ".join(state.steps[0].problems)
    assert len(state.steps) <= 3  # one try and at most two bounded retries, then a clear failure


def test_a_missing_element_without_a_replanner_fails_with_the_reason(kit: Kit) -> None:
    state = kit.run(task(act("navigate", value=BOOKS), act("click", target="button:Checkout")))
    assert state.status == FAILED and "Checkout" in state.reason


def test_failure_conditions_stop_the_task(kit: Kit, web: FakeWeb) -> None:
    web.pages[BOOKS] = Page("Access denied", "Access denied: robots are not welcome")
    state = kit.run(
        task(
            act("navigate", value=BOOKS),
            act("click", target="link:Contact"),
            failure_conditions=[{"text": "Access denied"}],
        )
    )
    assert state.status == FAILED and "failure condition" in state.reason


# ----------------------------------------------------------- control flow
def test_conditional_steps_follow_the_page(kit: Kit, web: FakeWeb) -> None:
    web.pages[BOOKS].controls.insert(
        0, Ctl("button", "Accept cookies", on_click=lambda w: w.pages[BOOKS].controls.pop(0))
    )
    branch = {"if": {"element": "button:Accept cookies"}, "then": [act("click", target="button:Accept cookies")]}
    first = kit.run(task(act("navigate", value=BOOKS), branch, act("click", target="link:Contact")))
    assert first.status == COMPLETED and "click Accept cookies" in web.calls
    web.calls.clear()
    second = kit.run(task(act("navigate", value=BOOKS), branch, act("click", target="link:Contact")))
    assert second.status == COMPLETED and "click Accept cookies" not in web.calls  # no banner the second time


def test_loops_repeat_until_the_condition_holds(kit: Kit, web: FakeWeb) -> None:
    state = kit.run(
        task(
            act("navigate", value=BOOKS),
            {"repeat": {"steps": [act("scroll", value="down")], "until": {"element": "link:Weekly deals"}, "max": 5}},
            act("click", target="link:Weekly deals"),
        )
    )
    assert state.status == COMPLETED
    assert web.calls.count("scroll down") == 1


def test_a_loop_that_never_gets_there_stops_at_its_limit(kit: Kit) -> None:
    state = kit.run(
        task(
            act("navigate", value=BOOKS),
            {"repeat": {"steps": [act("wait", value="0")], "until": {"text": "never"}, "max": 3}},
        )
    )
    assert state.status == FAILED and "3 times" in state.reason


# ----------------------------------------------------------- limits
def test_the_step_limit_prevents_endless_runs(kit: Kit) -> None:
    body = [act("scroll", value="down"), act("scroll", value="up")]
    state = kit.run(
        task(
            act("navigate", value=BOOKS),
            {"repeat": {"steps": body, "until": {"text": "never"}, "max": 25}},
            constraints={"max_steps": 8},
        )
    )
    assert state.status == FAILED and len(state.steps) <= 8
    assert "step limit" in state.reason or "already ran" in state.reason


def test_the_timeout_ends_the_task(kit: Kit) -> None:
    state = kit.run(
        task(act("navigate", value=BOOKS), act("wait", value="2"), act("wait", value="2"), constraints={"timeout": 1.5})
    )
    assert state.status == FAILED and "timed out" in state.reason


def test_cancellation_stops_the_task(kit: Kit) -> None:
    threading.Timer(0.2, kit.runtime.cancel.cancel).start()
    state = kit.run(task(act("navigate", value=BOOKS), act("wait", value="5")))
    assert state.status == CANCELLED
