"""Natural language → HXIR → plan, through the decider: understand broadly, resolve precisely,
ask when unclear — and leave everything the grammar already resolved exactly as it was."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.resolver import ResolverContext, _plan
from highhx.decision.deterministic import PRO, UNKNOWN, Decision, DeterministicDecider
from highhx.language import hxir
from highhx.language.hxir import Entity
from highhx.language.references import ConversationMemory

NOW = datetime(2026, 9, 30, 15, 0)
YESTERDAY = datetime(2026, 9, 29, 11, 0).timestamp()
LAST_MONTH = datetime(2026, 8, 10, 9, 0).timestamp()
REMOTES = ("https://github.com/me/highhxcli", "https://gitlab.com/me/mirror")


def touch(root: Path, rel: str, stamp: float) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (stamp, stamp))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "cli_project"
    touch(root, "docs/architecture.md", YESTERDAY)
    touch(root, "docs/old-architecture.pdf", LAST_MONTH)
    touch(root, "reports/q2-report.pdf", LAST_MONTH)
    touch(root, "reports/q3-report.pdf", YESTERDAY)
    touch(root, "README.md", LAST_MONTH)
    touch(root, ".env", YESTERDAY)
    return root


@pytest.fixture
def ctx(project: Path) -> ResolverContext:
    return ResolverContext(root=project, services=("backend",), _repositories=REMOTES)


def decide(ctx: ResolverContext, text: str, memory: ConversationMemory | None = None) -> Decision:
    return DeterministicDecider(ctx, memory=memory, now=NOW).decide(text)


def steps(decision: Decision) -> list[tuple[str, dict[str, Any]]]:
    assert decision.plan is not None, (decision.route, decision.reason, decision.unknown)
    return [(s.catalog_action, s.params) for s in decision.plan.steps]


# ------------------------------------------------------------- compatibility
@pytest.mark.parametrize(
    "text",
    [
        "open github",
        "run the tests",
        "open github and open my repository",
        "list files",
        "open README.md",
        "git status",
    ],
)
def test_requests_the_grammar_resolved_are_unchanged(ctx: ResolverContext, text: str) -> None:
    decision = decide(ctx, text)
    before, _ = _plan(text, ctx)
    assert before is not None and decision.resolution is not None
    assert decision.resolution.steps == before.steps and decision.rule == before.rule
    assert decision.hxir is not None and decision.hxir.status == hxir.RESOLVED
    assert [a.action for a in decision.hxir.actions] == [s.action for s in before.steps]


@pytest.mark.parametrize(
    "text", ["Run the tests and fix the failures.", "Why is the application crashing?", "Deploy this application."]
)
def test_open_ended_requests_still_go_to_pro(ctx: ResolverContext, text: str) -> None:
    decision = decide(ctx, text)
    assert decision.route == PRO and decision.plan is None
    assert decision.hxir is not None and decision.hxir.status == hxir.OPEN_ENDED


# --------------------------------------------------------------- phrasings
@pytest.mark.parametrize(
    "text", ["open github pls", "can u open github", "could you open github?", "take me to github", "Open GitHub."]
)
def test_phrasings_of_one_request(ctx: ResolverContext, text: str) -> None:
    assert steps(decide(ctx, text)) == [("browser.open", {"url": "https://github.com"})]


@pytest.mark.parametrize(
    "text",
    [
        "Open my repo.",
        "Open my HighhX project.",
        "open the highhxcli repository",
        "Take me to the project I'm working on.",
        "Open the current repository.",
    ],
)
def test_the_project(ctx: ResolverContext, text: str) -> None:
    assert steps(decide(ctx, text)) == [("filesystem.open", {"path": "."})]


def test_project_name_evidence_is_recorded(ctx: ResolverContext) -> None:
    decision = decide(ctx, "open my highhx project")
    entity = decision.hxir.entities[0]  # type: ignore[union-attr]
    assert entity.kind == "project" and entity.confidence == "medium" and "highhxcli" in entity.evidence


def test_an_unknown_project_is_not_guessed(ctx: ResolverContext) -> None:
    decision = decide(ctx, "open my payroll project")
    assert decision.route == UNKNOWN and decision.hxir.status == hxir.MISSING  # type: ignore[union-attr]
    assert "only knows the project it is running in" in decision.unknown.reason  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "text", ["Open my repository on GitHub.", "open my github repo", "take me to the repository on github"]
)
def test_repository_pages_come_from_git_remotes(ctx: ResolverContext, text: str) -> None:
    decision = decide(ctx, text)
    assert steps(decision) == [("browser.open", {"url": "https://github.com/me/highhxcli"})]
    assert decision.hxir.goal_type == "open_repository"  # type: ignore[union-attr]
    assert decision.hxir.entities[0].source == "git_remote"  # type: ignore[union-attr]


def test_no_remote_on_that_site_is_missing_information(ctx: ResolverContext) -> None:
    decision = decide(ctx, "open my repository on bitbucket")
    assert decision.route == UNKNOWN and "no Bitbucket remote" in decision.unknown.reason  # type: ignore[union-attr]


def test_run_the_tests_here(ctx: ResolverContext) -> None:
    decision = decide(ctx, "Run the tests here.")
    assert steps(decision) == [("project.test", {})]
    assert decision.hxir.references[0].kind == "place"  # type: ignore[union-attr]


def test_the_browser_is_asked_when_several_are_installed(ctx: ResolverContext, monkeypatch: pytest.MonkeyPatch) -> None:
    installed = {"Google Chrome", "Safari"}
    monkeypatch.setattr("highhx.computer.desktop.app_installed", lambda name: name in installed)
    decision = decide(ctx, "Start the browser.")
    assert decision.route == UNKNOWN and decision.hxir.status == hxir.AMBIGUOUS  # type: ignore[union-attr]
    assert decision.unknown.suggestions == ("1. Google Chrome  (installed)", "2. Safari  (installed)")  # type: ignore[union-attr]
    installed.discard("Safari")
    assert steps(decide(ctx, "start the browser")) == [("computer.launch", {"name": "Google Chrome"})]


def test_switch_to_the_browser_is_never_a_git_checkout(ctx: ResolverContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("highhx.computer.desktop.app_installed", lambda name: name == "Safari")
    assert steps(decide(ctx, "switch to the browser")) == [("computer.focus", {"app": "Safari"})]
    assert steps(decide(ctx, "switch to branch browser")) == [("git.checkout", {"ref": "browser"})]


# ------------------------------------------------------------------- files
def test_find_my_pdf_runs_the_find_action(ctx: ResolverContext) -> None:
    decision = decide(ctx, "Find my PDF.")
    assert steps(decision) == [("filesystem.find", {"sort": "newest", "limit": 20, "kinds": ["pdf"]})]
    assert decision.plan.steps[0].risk == "safe"  # type: ignore[union-attr]


def test_find_with_time_topic_and_order(ctx: ResolverContext) -> None:
    decision = decide(ctx, "Find the architecture document I was working on yesterday")
    [(action, inputs)] = steps(decision)
    assert action == "filesystem.find" and inputs["keywords"] == ["architecture"] and inputs["limit"] == 1
    assert inputs["modified_after"] == "2026-09-29T00:00:00" and inputs["modified_before"] == "2026-09-30T00:00:00"
    assert decision.hxir.entities[0].value == "docs/architecture.md"  # type: ignore[union-attr]
    kinds = {k for k, _ in decision.hxir.constraints}  # type: ignore[union-attr]
    assert {"file_type", "time", "recency"} <= kinds


def test_show_me_the_latest_report(ctx: ResolverContext) -> None:
    decision = decide(ctx, "Show me the latest report.")
    assert steps(decision)[0][1]["keywords"] == ["report"]
    assert decision.hxir.entities[0].value == "reports/q3-report.pdf"  # type: ignore[union-attr]


def test_open_by_description_needs_one_match(ctx: ResolverContext) -> None:
    assert steps(decide(ctx, "open the latest report")) == [("filesystem.open", {"path": "reports/q3-report.pdf"})]
    ambiguous = decide(ctx, "open the pdf")
    assert ambiguous.route == UNKNOWN and ambiguous.hxir.status == hxir.AMBIGUOUS  # type: ignore[union-attr]
    assert len(ambiguous.hxir.ambiguities[0].candidates) == 3  # type: ignore[union-attr]
    missing = decide(ctx, "open the spreadsheet")
    assert missing.hxir.status == hxir.MISSING  # type: ignore[union-attr]


def test_secret_files_are_never_found(ctx: ResolverContext) -> None:
    decision = decide(ctx, "open the env file")
    assert decision.plan is None
    assert all(".env" not in e.value for e in decision.hxir.entities)  # type: ignore[union-attr]


def test_downloads_are_outside_the_project_and_say_so(ctx: ResolverContext) -> None:
    decision = decide(ctx, "Find the PDF I downloaded yesterday.")
    assert decision.route == UNKNOWN and decision.hxir.status == hxir.UNSUPPORTED  # type: ignore[union-attr]
    assert "outside this project" in decision.unknown.reason and decision.plan is None  # type: ignore[union-attr]


def test_a_bare_ordinal_never_searches_the_whole_project(ctx: ResolverContext) -> None:
    decision = decide(ctx, "Open the second file.")
    assert decision.plan is None and decision.hxir.status == hxir.MISSING  # type: ignore[union-attr]


def test_execution_constraints_are_enforced(ctx: ResolverContext) -> None:
    fine = decide(ctx, "Find the latest PDF, but don't open anything.")
    assert [a for a, _ in steps(fine)] == ["filesystem.find"]
    contradiction = decide(ctx, "open the latest report but don't open anything")
    assert contradiction.plan is None and contradiction.hxir.status == hxir.INVALID  # type: ignore[union-attr]
    assert "forbids" in contradiction.unknown.reason  # type: ignore[union-attr]
    no_changes = decide(ctx, "run the tests without changing anything")
    assert no_changes.plan is None and no_changes.hxir.status == hxir.INVALID  # type: ignore[union-attr]


# -------------------------------------------------------- the conversation
def test_references_without_context_ask_instead_of_guessing(ctx: ResolverContext) -> None:
    for text in ("Open it.", "Open that.", "Go back there.", "Open the file we were working on."):
        decision = decide(ctx, text)
        assert decision.plan is None and decision.hxir.status == hxir.MISSING, text  # type: ignore[union-attr]
        assert "don't know an app" not in decision.unknown.reason  # type: ignore[union-attr]


def test_find_then_open_it_in_one_request(ctx: ResolverContext) -> None:
    decision = decide(ctx, "find the architecture document from yesterday and open it")
    assert steps(decision) == [
        ("filesystem.find", steps(decision)[0][1]),
        ("filesystem.open", {"path": "docs/architecture.md"}),
    ]
    assert decision.hxir.actions[1].depends_on == ("a1",)  # type: ignore[union-attr]
    assert decision.hxir.references[0].status == hxir.RESOLVED  # type: ignore[union-attr]


def test_open_it_with_one_clear_referent(ctx: ResolverContext) -> None:
    memory = ConversationMemory()
    memory.record("open the readme", [Entity("file", "README.md")])
    assert steps(decide(ctx, "open it", memory)) == [("filesystem.open", {"path": "README.md"})]
    assert steps(decide(ctx, "open it in safari", memory)) == [
        ("filesystem.open", {"path": "README.md", "app": "Safari"})
    ]


def test_open_it_with_several_referents_asks(ctx: ResolverContext) -> None:
    memory = ConversationMemory()
    memory.record(
        "open github and open my repository",
        [Entity("website", "https://github.com", "GitHub"), Entity("url", "https://github.com/me/highhxcli")],
    )
    decision = decide(ctx, "open it", memory)
    assert decision.route == UNKNOWN and decision.hxir.status == hxir.AMBIGUOUS  # type: ignore[union-attr]
    assert decision.unknown.suggestions == ("1. GitHub", "2. https://github.com/me/highhxcli")  # type: ignore[union-attr]


def test_answering_the_question_completes_the_request(ctx: ResolverContext) -> None:
    memory = ConversationMemory()
    question = decide(ctx, "open the pdf", memory)
    ambiguity = question.hxir.ambiguities[0]  # type: ignore[union-attr]
    memory.ask(question.hxir.request, ambiguity.template, ambiguity.candidates)  # type: ignore[union-attr]
    second = ambiguity.candidates[1].value
    for answer in ("the second one", "2", "use the second one", "#2", second):
        assert steps(decide(ctx, answer, memory)) == [("filesystem.open", {"path": second})], answer
    assert decide(ctx, "the ninth one", memory).hxir.status == hxir.MISSING  # type: ignore[union-attr]
    assert decide(ctx, "open it", memory).hxir.status == hxir.AMBIGUOUS  # type: ignore[union-attr]


def test_ordinals_over_a_listing(ctx: ResolverContext) -> None:
    memory = ConversationMemory()
    listed = [Entity("file", "README.md"), Entity("folder", "docs"), Entity("folder", "reports")]
    memory.record("list files", listed, listed)
    assert steps(decide(ctx, "open the second one", memory)) == [("filesystem.open", {"path": "docs"})]
    assert steps(decide(ctx, "show me the last one", memory)) == [("filesystem.open", {"path": "reports"})]


def test_the_previous_page(ctx: ResolverContext) -> None:
    memory = ConversationMemory()
    memory.record("open github", [Entity("website", "https://github.com", "GitHub")])
    memory.record("open youtube", [Entity("website", "https://www.youtube.com", "YouTube")])
    assert steps(decide(ctx, "go back to the previous page", memory)) == [
        ("browser.open", {"url": "https://github.com"})
    ]
    assert steps(decide(ctx, "go back there", memory)) == [("browser.open", {"url": "https://www.youtube.com"})]


def test_corrections(ctx: ResolverContext) -> None:
    memory = ConversationMemory()
    memory.record("open youtube", [Entity("website", "https://www.youtube.com", "YouTube")])
    meant = decide(ctx, "No, I meant GitHub.", memory)
    assert steps(meant) == [("browser.open", {"url": "https://github.com"})]
    assert meant.hxir.correction_of == "open youtube"  # type: ignore[union-attr]
    memory.record("open github", [Entity("website", "https://github.com", "GitHub")])
    assert steps(decide(ctx, "Actually open my repository.", memory)) == [("filesystem.open", {"path": "."})]
    assert decide(ctx, "no, I meant GitHub", None).hxir.status == hxir.MISSING  # type: ignore[union-attr]


def test_the_compound_github_task_is_understood_as_far_as_it_is_deterministic(ctx: ResolverContext) -> None:
    decision = decide(
        ctx, "Open GitHub, find my HighhX repository, check the issues assigned to me, and open the first one."
    )
    assert decision.route == PRO and decision.plan is None  # reading the issues needs the page (Pro)
    clauses = decision.hxir.clauses  # type: ignore[union-attr]
    assert [c.status for c in clauses] == [hxir.RESOLVED, hxir.RESOLVED, hxir.OPEN_ENDED]
    assert any(e.value == "https://github.com/me/highhxcli" for e in decision.hxir.entities)  # type: ignore[union-attr]
    assert "1 later step(s) not examined" in decision.hxir.reason  # type: ignore[union-attr]


def test_hxir_is_valid_and_deterministic(ctx: ResolverContext) -> None:
    from highhx.actions.catalog import default_catalog

    for text in ("find my pdf and open it", "open the pdf", "open it", "Deploy this application.", "open github"):
        first, second = decide(ctx, text).hxir, decide(ctx, text).hxir
        assert first is not None and second is not None
        assert first.to_json() == second.to_json()
        assert hxir.validate(first.to_dict(redact=False), default_catalog()) == [], text
