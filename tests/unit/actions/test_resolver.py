"""Deterministic intent resolution: complete matches with known entities, never guesses."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from highhx.actions.catalog import default_catalog
from highhx.actions.resolver import RULES, ResolverContext, normalise, resolve

CTX = ResolverContext(
    services=("backend", "frontend", "worker"),
    deploy_targets=("staging", "production"),
    environments=("dev",),
    workflows=("ci", "release"),
)


@pytest.mark.parametrize(
    ("text", "steps"),
    [
        ("run the tests", [("project.test", {})]),
        ("Run all the tests.", [("project.test", {})]),
        ("please run tests", [("project.test", {})]),
        ("show git status", [("git.status", {})]),
        ("git status", [("git.status", {})]),
        ("what changed", [("git.diff", {})]),
        ("show recent commits", [("git.log", {})]),
        ("build the project", [("project.build", {})]),
        ("run the checks", [("project.check", {})]),
        ("format the code", [("project.fix", {})]),
        ("install dependencies", [("package.install", {})]),
        ("update the dependencies", [("package.update", {})]),
        ("show outdated packages", [("package.outdated", {})]),
        ("start the server", [("service.start", {})]),
        ("stop the backend", [("service.stop", {"services": ["backend"]})]),
        ("restart the worker", [("service.restart", {"services": ["worker"]})]),
        ("check security", [("security.scan", {})]),
        ("scan for secrets", [("security.scan", {})]),
        ("run the doctor", [("security.doctor", {})]),
        ("show recent logs", [("service.logs", {})]),
        ("show backend logs", [("service.logs", {"service": "backend"})]),
        ("deploy staging", [("deployment.deploy", {"environment": "staging"})]),
        ("deploy to prod", [("deployment.deploy", {"environment": "production"})]),
        ("roll back staging", [("deployment.rollback", {"environment": "staging"})]),
        ("show deployment status", [("deployment.status", {})]),
        ("open localhost 3000", [("browser.open", {"url": "http://localhost:3000"})]),
        ("open localhost:8080/admin", [("browser.open", {"url": "http://localhost:8080/admin"})]),
        ("go to example.com", [("browser.open", {"url": "https://example.com"})]),
        ("run the ci workflow", [("workflow.run", {"name": "ci"})]),
        ("run workflow release", [("workflow.run", {"name": "release"})]),
        ("resume workflow 20260927abcdef", [("workflow.resume", {"execution_id": "20260927abcdef"})]),
        ("create branch feature/login", [("git.branch", {"name": "feature/login"})]),
        ("switch to main", [("git.checkout", {"ref": "main"})]),
        ('commit all changes with message "fix login"', [("git.commit", {"message": "fix login", "all": True})]),
        ("tag v1.2.3", [("git.tag", {"name": "v1.2.3"})]),
        ("push", [("git.push", {})]),
        ("migrate the database", [("database.migrate", {})]),
        ("back up the database", [("database.backup", {})]),
        ("search for 'TODO'", [("filesystem.search", {"pattern": "TODO"})]),
        ("take a screenshot", [("browser.screenshot", {})]),
        ("click the Save button", [("browser.click", {"target": "button:Save"})]),
        ("run the tests and then build", [("project.test", {}), ("project.build", {})]),
        ("install deps, then run the tests", [("package.install", {}), ("project.test", {})]),
    ],
)
def test_resolves(text: str, steps: list[tuple[str, dict[str, Any]]]) -> None:
    resolution = resolve(text, CTX)
    assert resolution is not None, text
    assert [(s.action, s.inputs) for s in resolution.steps] == steps


@pytest.mark.parametrize(
    "text",
    [
        "fix the failing tests",
        "Fix the failing tests.",
        "find why the API is slow and fix it",
        "inspect this project and tell me what needs improvement",
        "create a REST API for this feature",
        "run the tests, fix failures, and verify everything",
        "clean up unused code and show me the changes",
        "prepare this repository for production",
        "fix",
        "deploy",
        "deploy this application",
        "deploy to the moon",
        "stop the database",  # no service with that name
        "open the dashboard in the browser and click export",
        "open main.py",  # not a website, and not an existing file here
        "change README",
        "start fixing bugs",
        "run the tests and fix them",  # all or nothing: "fix them" is open-ended
        "",
    ],
)
def test_does_not_pretend_to_understand(text: str) -> None:
    assert resolve(text, CTX) is None


def test_files_resolve_only_when_they_exist(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# hi\n")
    ctx = ResolverContext(root=tmp_path)
    resolution = resolve("read README.md", ctx)
    assert resolution is not None and resolution.steps[0].inputs == {"path": "README.md"}
    assert resolve("read MISSING.md", ctx) is None
    assert resolve("open ../../etc/passwd", ctx) is None


def test_no_environments_configured_accepts_common_names_only() -> None:
    bare = ResolverContext()
    assert resolve("deploy staging", bare) is not None
    assert resolve("deploy banana", bare) is None


def test_deterministic_and_normalised() -> None:
    first = resolve("Please run the tests!", CTX)
    assert first == resolve("Please run the tests!", CTX) and first is not None
    assert normalise("  can you   run the tests? ") == "run the tests"


def test_every_rule_targets_a_catalog_action() -> None:
    catalog = default_catalog()
    for text in ("run the tests", "show git status", "migrate the database", "take a screenshot"):
        resolution = resolve(text, CTX)
        assert resolution is not None and all(s.action in catalog for s in resolution.steps)
    assert len({r.name for r in RULES}) == len(RULES)  # unique rule names (reported in events)
