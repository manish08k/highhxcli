"""Checks and screenshots in plain language: "… and verify the window", "take a screenshot of
TextEdit". A check is its own clause (it once became part of the previous clause's target), and
where it looks — the desktop or the browser page — follows what the earlier clauses established."""

from __future__ import annotations

from typing import Any

import pytest

from highhx.actions.catalog import default_catalog
from highhx.actions.resolver import ResolverContext, explain, resolve

CTX = ResolverContext()
WINDOW = [{"window": {"exists": True}}]


def steps(text: str) -> list[tuple[str, dict[str, Any]]]:
    resolution = resolve(text, CTX)
    assert resolution is not None, text
    return [(s.action, dict(s.inputs)) for s in resolution.steps]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "switch to Notes, click Save and verify the window",
            [
                ("computer.focus", {"app": "Notes"}),
                ("computer.click", {"target": "button:Save"}),
                ("computer.verify", {"app": "Notes", "expect": WINDOW}),
            ],
        ),
        ("verify the TextEdit window is open", [("computer.verify", {"app": "TextEdit", "expect": WINDOW})]),
        (
            "open TextEdit, type hello and confirm hello is shown",
            [
                ("computer.launch", {"name": "TextEdit"}),
                ("computer.type", {"text": "hello"}),
                (
                    "computer.verify",
                    {"app": "TextEdit", "expect": [{"text": {"contains": "hello"}}]},
                ),
            ],
        ),
        (
            "switch to Notes and make sure the Save button is visible",
            [
                ("computer.focus", {"app": "Notes"}),
                (
                    "computer.verify",
                    {
                        "app": "Notes",
                        "expect": [
                            {"element": {"selector": {"label_contains": "Save", "role": "button"}, "exists": True}}
                        ],
                    },
                ),
            ],
        ),
        (
            "open youtube and verify Trending is shown",
            [("browser.open", {"url": "https://www.youtube.com"}), ("browser.wait", {"text": "Trending"})],
        ),
    ],
)
def test_checks(text: str, expected: list[tuple[str, dict[str, Any]]]) -> None:
    assert steps(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("take a screenshot", [("browser.screenshot", {})]),  # unchanged: the page, by default
        ("screenshot of the page", [("browser.screenshot", {})]),
        ("capture the screen", [("computer.screenshot", {})]),
        ("take a screenshot of the screen", [("computer.screenshot", {})]),
        ("take a screenshot of the TextEdit window", [("computer.screenshot", {"app": "TextEdit"})]),
        (
            "open TextEdit and take a screenshot",
            [("computer.launch", {"name": "TextEdit"}), ("computer.screenshot", {"app": "TextEdit"})],
        ),
    ],
)
def test_screenshots(text: str, expected: list[tuple[str, dict[str, Any]]]) -> None:
    assert steps(text) == expected


def test_what_cannot_be_resolved_is_explained_not_guessed() -> None:
    assert resolve("verify the window", CTX) is None  # which application's?
    unknown = explain("verify the window", CTX)
    assert unknown is not None and "which application" in unknown.reason
    assert resolve("take a screenshot of Nonexistent Thing", CTX) is None


def test_every_new_step_is_valid_catalog_input() -> None:
    catalog = default_catalog()
    for text in ("switch to Notes, click Save and verify the window", "take a screenshot of the TextEdit window"):
        for action, inputs in steps(text):
            spec = catalog.get(action)
            assert spec is not None and spec.validate(inputs) == [], (action, inputs)
