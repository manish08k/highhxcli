"""Declarative verification: every check type, the combinators, unknown ≠ success, re-sampling."""

from __future__ import annotations

from pathlib import Path

import pytest

from highhx.actions.spec import ActionResult
from highhx.core.errors import ValidationError
from highhx.perception.state import BrowserState, ComputerState, ScreenshotRef, StateElement
from highhx.verification.declarative import (
    CHECKS,
    Verdict,
    VerificationContext,
    evaluate,
    validate,
    verify,
)
from tests.unit.perception.fakes import png


def _state(**kw) -> ComputerState:
    kw.setdefault("browser", BrowserState("https://shop.test/orders/42", "Order 42"))
    return ComputerState("browser", **kw)


def test_element_checks_by_source() -> None:
    state = _state(
        elements=(
            StateElement("e1", "button", "Submit", sources=("dom",)),
            StateElement("a1", "checkbox", "Agree", checked=True, sources=("ax",)),
            StateElement("p", "textbox", "Password", value="", attributes=(("type", "password"),)),
        )
    )
    ctx = VerificationContext(after=state)
    assert evaluate({"element": {"role": "button", "name": "submit"}}, ctx).satisfied
    assert evaluate({"dom": {"name": "Submit"}}, ctx).satisfied
    assert evaluate({"dom": {"name": "Agree"}}, ctx).verdict == Verdict.UNSATISFIED
    assert evaluate({"accessibility": {"name": "Agree", "checked": True}}, ctx).satisfied
    assert evaluate({"accessibility": {"name": "Agree", "checked": False}}, ctx).verdict == Verdict.UNSATISFIED
    assert evaluate({"element": {"name": "Password", "value": "x"}}, ctx).verdict == Verdict.UNKNOWN
    assert evaluate({"absent": {"role": "button", "name": "Cancel"}}, ctx).satisfied
    assert evaluate({"absent": "Submit"}, ctx).verdict == Verdict.UNSATISFIED


def test_text_url_title_application_process() -> None:
    state = _state(text="Thank you for your order", active_app="Chrome", processes=("Chrome", "Finder"))
    ctx = VerificationContext(after=state)
    assert evaluate({"text": "thank you"}, ctx).satisfied
    assert evaluate({"text_absent": "error"}, ctx).satisfied
    assert evaluate({"url_contains": "/orders/"}, ctx).satisfied
    assert evaluate({"url_matches": r"/orders/\d+$"}, ctx).satisfied
    assert evaluate({"title_contains": "order 42"}, ctx).satisfied
    assert evaluate({"application": "chrome"}, ctx).satisfied
    assert evaluate({"application": {"name": "Chrome", "active": False}}, ctx).verdict == Verdict.UNSATISFIED
    assert evaluate({"process": {"name": "Finder"}}, ctx).satisfied
    assert evaluate({"process": {"name": "Slack", "running": False}}, ctx).satisfied
    desktop = ComputerState("desktop")
    assert evaluate({"url_contains": "x"}, VerificationContext(after=desktop)).verdict == Verdict.UNSUPPORTED
    assert evaluate({"process": "x"}, VerificationContext(after=desktop)).verdict == Verdict.UNSUPPORTED


def test_exit_code_and_files(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("built ok\n")
    ctx = VerificationContext(result=ActionResult(True, output={"exit_code": 0}), root=tmp_path)
    assert evaluate({"exit_code": 0}, ctx).satisfied
    assert evaluate({"exit_code": 1}, ctx).verdict == Verdict.UNSATISFIED
    assert evaluate({"file": {"path": "out.txt", "contains": "ok"}}, ctx).satisfied
    assert evaluate({"file": {"path": "out.txt", "not_contains": "ok"}}, ctx).verdict == Verdict.UNSATISFIED
    assert evaluate({"file": {"path": "gone.txt", "exists": False}}, ctx).satisfied
    assert evaluate({"file": {"path": "gone.txt"}}, ctx).verdict == Verdict.UNSATISFIED
    no_code = VerificationContext(result=ActionResult(True))
    assert evaluate({"exit_code": 0}, no_code).satisfied


def test_changed_and_visual() -> None:
    before = _state(text="a", screenshot=ScreenshotRef.from_bytes(png()))
    after = _state(text="b", screenshot=ScreenshotRef.from_bytes(png(boxes=((10, 10, 30, 30),))))
    ctx = VerificationContext(before=before, after=after)
    assert evaluate({"changed": True}, ctx).satisfied
    assert evaluate({"visual": {"changed": True}}, ctx).satisfied
    same = VerificationContext(before=before, after=before)
    assert evaluate({"visual": {"changed": False}}, same).satisfied
    no_pixels = VerificationContext(before=_state(), after=_state())
    assert evaluate({"visual": True}, no_pixels).verdict == Verdict.UNKNOWN


def test_network_and_custom() -> None:
    log = [{"url": "https://api.test/orders", "method": "POST", "status": 201}]
    ctx = VerificationContext(network=log, custom={"even": lambda _c, v: (v["n"] % 2 == 0, "parity")})
    assert evaluate({"network": {"url_contains": "/orders", "status": 201, "method": "post"}}, ctx).satisfied
    assert evaluate({"network": {"url_contains": "/pay"}}, ctx).verdict == Verdict.UNSATISFIED
    assert evaluate({"network": {"status": 200}}, VerificationContext()).verdict == Verdict.UNKNOWN
    assert evaluate({"custom": {"name": "even", "n": 4}}, ctx).satisfied
    assert evaluate({"custom": {"name": "missing"}}, ctx).verdict == Verdict.UNKNOWN


def test_combinators_never_count_unknown_as_success() -> None:
    ctx = VerificationContext(after=_state(text="done"))
    unknown = {"network": {"status": 200}}
    assert evaluate({"all": [{"text": "done"}, unknown]}, ctx).verdict == Verdict.UNKNOWN
    assert evaluate({"all": [{"text": "done"}, {"text": "nope"}, unknown]}, ctx).verdict == Verdict.UNSATISFIED
    assert evaluate({"any": [unknown, {"text": "done"}]}, ctx).satisfied
    assert evaluate({"any": [unknown, {"text": "nope"}]}, ctx).verdict == Verdict.UNKNOWN
    assert evaluate({"not": {"text": "nope"}}, ctx).satisfied
    assert evaluate({"not": unknown}, ctx).verdict == Verdict.UNKNOWN
    assert evaluate([{"text": "done"}], ctx).satisfied  # a list means all


def test_validation_and_errors() -> None:
    assert validate({"any": [{"text": "x"}, {"bogus": 1}]}) == ["verification.any[1]: unknown check 'bogus'"]
    assert validate({"all": []}) and validate({"a": 1, "b": 2})
    with pytest.raises(ValidationError):
        evaluate({"bogus": 1}, VerificationContext())
    assert {"exit_code", "file", "dom", "accessibility", "text", "visual", "network", "process", "custom"} <= set(CHECKS)


def test_verify_resamples_until_satisfied() -> None:
    states = iter([_state(text="loading"), _state(text="loading"), _state(text="done")])
    ctx = VerificationContext(after=_state(text="loading"), observe=lambda: next(states))
    report = verify({"text": "done"}, ctx, timeout=5, interval=0, sleep=lambda _s: None)
    assert report.satisfied and report.samples == 4
    stuck = VerificationContext(after=_state(text="x"), observe=lambda: _state(text="x"))
    report = verify({"text": "done"}, stuck, timeout=0.05, interval=0.01)
    assert report.verdict == Verdict.UNSATISFIED and report.samples >= 2


def test_partial_reports() -> None:
    ctx = VerificationContext(after=_state(text="done"))
    report = verify({"all": [{"text": "done"}, {"text": "nope"}]}, ctx)
    assert report.partial and report.to_dict()["partial"] is True


def test_unsupported_is_distinct_from_unconfirmed() -> None:
    desktop = ComputerState("desktop", active_app="Notes")
    ctx = VerificationContext(after=desktop)
    assert evaluate({"url_contains": "x"}, ctx).verdict == Verdict.UNSUPPORTED
    assert evaluate({"android": {"focused_app": "x"}}, ctx).verdict == Verdict.UNSUPPORTED
    assert evaluate({"url_contains": "x"}, VerificationContext()).verdict == Verdict.UNKNOWN
    assert evaluate({"all": [{"application": "Notes"}, {"url_contains": "x"}]}, ctx).verdict == Verdict.UNSUPPORTED
    assert evaluate({"any": [{"url_contains": "x"}, {"application": "Notes"}]}, ctx).satisfied
    assert evaluate({"not": {"url_contains": "x"}}, ctx).verdict == Verdict.UNSUPPORTED  # never flipped into success
    assert evaluate({"http_response": {"status": 200}}, VerificationContext(result=ActionResult(True))).verdict == Verdict.UNSUPPORTED


def test_text_changed_file_hash_http_sqlite_and_android(tmp_path: Path) -> None:
    import hashlib
    import sqlite3

    before = _state(text="Draft saved 10:01")
    after = _state(text="Draft saved 10:02")
    assert evaluate({"text_changed": True}, VerificationContext(before=before, after=after)).satisfied
    assert evaluate({"text_changed": {"from": "10:01"}}, VerificationContext(before=before, after=after)).satisfied
    (tmp_path / "a.bin").write_bytes(b"abc")
    digest = hashlib.sha256(b"abc").hexdigest()
    assert evaluate({"file_hash": {"path": "a.bin", "sha256": digest}}, VerificationContext(root=tmp_path)).satisfied
    assert evaluate({"file_hash": {"path": "a.bin", "sha256": "0" * 64}}, VerificationContext(root=tmp_path)).verdict == Verdict.UNSATISFIED
    response = ActionResult(True, output={"status": 201, "body": '{"id": 7}', "json": {"order": {"id": 7}}})
    ctx = VerificationContext(result=response)
    assert evaluate({"http_response": {"status": 201, "contains": "id", "json": {"order.id": 7}}}, ctx).satisfied
    assert evaluate({"http_response": {"status": 200}}, ctx).verdict == Verdict.UNSATISFIED
    db = tmp_path / "app.db"
    import contextlib

    with contextlib.closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("create table orders (id integer)")
        connection.execute("insert into orders values (1), (2)")
    sql = VerificationContext(root=tmp_path)
    assert evaluate({"sqlite": {"path": "app.db", "query": "select count(*) from orders", "equals": 2}}, sql).satisfied
    assert evaluate({"sqlite": {"path": "app.db", "query": "select * from orders", "rows": 2}}, sql).satisfied
    with pytest.raises(ValidationError):
        evaluate({"sqlite": {"path": "app.db", "query": "delete from orders"}}, sql)
    phone = ComputerState("android", active_app="com.example.notes", elements=(StateElement("a1", "button", "Save", sources=("android",)),))
    assert evaluate({"android": {"focused_app": "com.example.notes", "element": {"name": "Save"}}}, VerificationContext(after=phone)).satisfied
    assert evaluate({"ui_state": {"name": "Save", "enabled": True}}, VerificationContext(after=phone)).satisfied
