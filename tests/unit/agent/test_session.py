"""The agent loop: model ↔ tools, plans, approvals, limits, persistence."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from highhx.agent.history import SessionStore
from highhx.agent.messages import ToolResultBlock
from highhx.agent.permissions import ApprovalMode
from highhx.agent.tools.base import Tool, ToolContext, ToolResult
from highhx.agent.tools.registry import ToolRegistry, builtin_tools
from highhx.core.errors import ModelProviderError
from tests.unit.agent.conftest import PY, RecordingUI, reply, retryable


def break_calc(root: Path) -> Path:
    calc = root / "src" / "pyapp" / "calc.py"
    calc.write_text("def add(a: int, b: int) -> int:\n    return a - b\n")
    return calc


def test_fixes_failing_tests_end_to_end(agent_project: Path, make_session) -> None:
    calc = break_calc(agent_project)
    session, provider, ui = make_session(
        agent_project,
        [
            reply("I'll run the tests first.", [("run_tests", {})]),
            reply("", [("read_file", {"path": "src/pyapp/calc.py"})]),
            reply(
                "",
                [("edit_file", {"path": "src/pyapp/calc.py", "old_text": "return a - b", "new_text": "return a + b"})],
            ),
            reply("", [("run_tests", {})]),
            reply("Fixed `add` (it subtracted). All tests pass."),
        ],
    )
    result = session.run_turn("fix my failing tests")

    assert result.stopped == "completed"
    assert result.text == "Fixed `add` (it subtracted). All tests pass."
    assert calc.read_text().endswith("return a + b\n")
    assert [name for name, _ok in result.tools] == ["run_tests", "read_file", "edit_file", "run_tests"]
    assert [ok for _name, ok in result.tools] == [False, True, True, True]
    assert result.changed_files == ["src/pyapp/calc.py"]
    # The failing run's output went back to the model so it could find the cause.
    first_results = provider.requests[1].messages[-1].tool_results
    assert first_results[0].is_error and "1 failed" in first_results[0].content
    finished = ui.of("tool_finished")
    assert finished[0][2] == "1 test failing"
    assert finished[-1][1] and finished[-1][2].startswith("Tests passing")
    # ask mode: the edit and the test runs were approved through the UI.
    assert any(a.startswith("Edit src/pyapp/calc.py") for a in ui.of("permission"))
    assert result.usage.input_tokens == 500


def test_turn_is_recorded_in_highhx_history(agent_project: Path, make_session) -> None:
    session, _provider, _ui = make_session(agent_project, [reply("", [("run_tests", {})]), reply("done")])
    session.run_turn("run the tests")
    history = session.app.history
    assert history is not None
    record = history.list(kind="agent")[0]
    assert record.name == "run the tests"
    assert record.status == "success"
    steps = history.get(record.id).steps
    assert any(step.step_id == "test" for step in steps)


def test_plan_is_presented_and_progress_tracked(agent_project: Path, make_session) -> None:
    session, provider, ui = make_session(
        agent_project,
        [
            reply("", [("propose_plan", {"goal": "Ship it", "steps": ["Run tests", "Check security"]})]),
            reply("", [("update_plan", {"step": 1, "status": "in_progress"})]),
            reply("", [("update_plan", {"step": 1, "status": "done", "note": "1 passed"})]),
            reply("", [("update_plan", {"step": 2, "status": "failed", "note": "2 findings"})]),
            reply("Done."),
        ],
    )
    session.run_turn("prepare this project for release")
    assert ui.of("plan") == ["Ship it"]
    assert session.plan is not None and session.plan.approved
    assert [s.status for s in session.plan.steps] == ["done", "failed"]
    assert session.plan.steps[0].note == "1 passed"
    assert ui.of("plan_updated") == [(0, "in_progress"), (0, "done"), (1, "failed")]
    assert "approved the plan" in provider.requests[1].messages[-1].tool_results[0].content
    assert session.record is not None and session.record.plan is not None


def test_declined_plan_returns_feedback(agent_project: Path, make_session) -> None:
    ui = RecordingUI(plan_answers=[(False, "skip the deploy step")])
    session, provider, _ = make_session(
        agent_project,
        [reply("", [("propose_plan", {"goal": "Release", "steps": ["Test", "Deploy"]})]), reply("OK, revised.")],
        ui=ui,
    )
    session.run_turn("release")
    result = provider.requests[1].messages[-1].tool_results[0]
    assert result.is_error and "skip the deploy step" in result.content
    assert session.plan is None


def test_declined_edit_is_not_applied(agent_project: Path, make_session) -> None:
    calc = agent_project / "src" / "pyapp" / "calc.py"
    before = calc.read_text()
    ui = RecordingUI(permission_answers=["no"])
    session, provider, _ = make_session(
        agent_project,
        [
            reply("", [("edit_file", {"path": "src/pyapp/calc.py", "old_text": "a + b", "new_text": "b + a"})]),
            reply("Understood."),
        ],
        ui=ui,
    )
    session.run_turn("swap operands")
    assert calc.read_text() == before
    result = provider.last_tool_results()[0]
    assert result.is_error and "did not approve" in result.content


def test_always_allow_grants_for_the_session(agent_project: Path, make_session) -> None:
    ui = RecordingUI(permission_answers=["always"])
    edit = {"path": "notes.md", "old_text": "", "new_text": "one\n"}
    session, _provider, _ = make_session(
        agent_project,
        [
            reply("", [("edit_file", edit)]),
            reply("", [("write_file", {"path": "notes.md", "content": "two\n"})]),
            reply("done"),
        ],
        ui=ui,
    )
    session.run_turn("write notes")
    assert (agent_project / "notes.md").read_text() == "two\n"
    assert len(ui.of("permission")) == 1


def test_auto_edit_mode_does_not_ask_for_normal_changes(agent_project: Path, make_session) -> None:
    session, _provider, ui = make_session(
        agent_project,
        [reply("", [("write_file", {"path": "a.txt", "content": "x"})]), reply("ok")],
        mode=ApprovalMode.AUTO_EDIT,
    )
    session.run_turn("write a")
    assert (agent_project / "a.txt").read_text() == "x"
    assert ui.of("permission") == []


def test_read_only_mode_offers_no_mutating_tools(agent_project: Path, make_session) -> None:
    session, provider, _ui = make_session(
        agent_project,
        [reply("", [("write_file", {"path": "a.txt", "content": "x"})]), reply("I can't change files.")],
        mode=ApprovalMode.READ_ONLY,
    )
    session.run_turn("write a")
    names = {t.name for t in provider.requests[0].tools}
    assert "read_file" in names and "write_file" not in names and "run_command" not in names
    assert not (agent_project / "a.txt").exists()
    assert "Unknown tool" in provider.last_tool_results()[0].content


def test_project_policy_blocks_agent_writes(agent_project: Path, make_session) -> None:
    (agent_project / ".highhx" / "policies.yaml").write_text(
        "rules:\n  - id: no-agent-writes\n    effect: deny\n    message: agents may not edit files here\n"
        "    when:\n      action: 'agent:write'\n"
    )
    session, provider, ui = make_session(
        agent_project, [reply("", [("write_file", {"path": "a.txt", "content": "x"})]), reply("Blocked.")]
    )
    session.run_turn("write a")
    assert not (agent_project / "a.txt").exists()
    result = provider.last_tool_results()[0]
    assert result.is_error and "policy" in result.content
    assert ui.of("permission") == []


def test_dangerous_command_needs_confirmation_with_full_details(agent_project: Path, make_session) -> None:
    ui = RecordingUI(action_answers=[False])
    session, provider, _ = make_session(
        agent_project,
        [reply("", [("run_command", {"command": "git push --force origin main"})]), reply("Not pushed.")],
        ui=ui,
        mode=ApprovalMode.AUTO_EDIT,  # auto-edit never covers sensitive actions
    )
    session.run_turn("push")
    request = ui.requests[0]
    assert request.command == "git push --force origin main" and request.tool == "run_command"
    assert request.risk.label == "critical" and request.irreversible and request.confirm_word == "approve"
    assert any("force push" in r for r in request.reasons)
    result = provider.last_tool_results()[0]
    assert result.is_error and "Cancelled" in result.content
    audit = session.permissions.gate.audit.list(session_id=session.record.id)
    assert audit[0].decision == "denied" and audit[0].status == "skipped"


def test_command_output_is_redacted_before_reaching_the_model(agent_project: Path, make_session) -> None:
    session, provider, _ = make_session(
        agent_project,
        [reply("", [("run_command", {"command": f"{PY} -c \"print('token=supersecretvalue123')\""})]), reply("ok")],
        mode=ApprovalMode.AUTO_EDIT,
    )
    session.app.redactor.add(["supersecretvalue123"])
    session.run_turn("print it")
    content = provider.last_tool_results()[0].content
    assert "supersecretvalue123" not in content
    assert "[REDACTED]" in content


def test_invalid_tool_input_is_rejected_before_running(agent_project: Path, make_session) -> None:
    session, provider, _ = make_session(
        agent_project,
        [reply("", [("read_file", {"path": 42})]), reply("", [("read_file", {"INVALID_JSON": "{"})]), reply("ok")],
    )
    session.run_turn("read")
    assert "expected a string" in provider.requests[1].messages[-1].tool_results[0].content
    assert "not valid JSON" in provider.requests[2].messages[-1].tool_results[0].content


def test_step_limit_stops_the_turn(agent_project: Path, make_session) -> None:
    session, _provider, ui = make_session(
        agent_project, [reply("", [("list_files", {})]) for _ in range(5)], max_steps=2
    )
    result = session.run_turn("loop forever")
    assert result.stopped == "max_steps"
    assert result.steps == 2
    assert any("Stopped after 2" in m for m in ui.of("notice:warn"))


def test_truncated_tool_call_is_not_executed(agent_project: Path, make_session) -> None:
    session, provider, _ = make_session(
        agent_project,
        [reply("", [("write_file", {"path": "big.txt", "content": "partial"})], stop="max_tokens"), reply("retrying")],
        mode=ApprovalMode.AUTO_EDIT,
    )
    session.run_turn("write a big file")
    assert not (agent_project / "big.txt").exists()
    assert "output limit" in provider.last_tool_results()[0].content


def test_refusal_ends_the_turn(agent_project: Path, make_session) -> None:
    session, _provider, ui = make_session(agent_project, [reply("I can't help with that.", stop="refusal")])
    result = session.run_turn("something bad")
    assert result.stopped == "refusal"
    assert ui.of("notice:warn")


def test_retryable_provider_errors_are_retried(agent_project: Path, make_session) -> None:
    session, provider, ui = make_session(agent_project, [retryable(), reply("hello")])
    result = session.run_turn("hi")
    assert result.text == "hello"
    assert len(provider.requests) == 2
    assert any("Retrying" in m for m in ui.of("notice:warn"))


def test_fatal_provider_errors_propagate(agent_project: Path, make_session) -> None:
    session, _provider, _ui = make_session(agent_project, [ModelProviderError("bad key")])
    with pytest.raises(ModelProviderError):
        session.run_turn("hi")
    assert session.record is not None and session.record.turns == 1


class _Interrupting(Tool):
    name = "interrupt"
    description = "raises KeyboardInterrupt"

    def run(self, ctx: ToolContext, args: dict) -> ToolResult:
        raise KeyboardInterrupt


def test_interrupted_turn_leaves_a_valid_transcript(agent_project: Path, make_session) -> None:
    session, provider, _ui = make_session(agent_project, [reply("", [("interrupt", {})]), reply("resumed")])
    session.registry = ToolRegistry([*builtin_tools(), _Interrupting()])
    result = session.run_turn("do it")
    assert result.stopped == "cancelled"
    last = session.messages[-1]
    assert last.role == "user" and isinstance(last.blocks[0], ToolResultBlock) and last.blocks[0].is_error
    session.run_turn("continue")
    calls = {c.id for m in provider.requests[-1].messages for c in m.tool_calls}
    answered = {r.tool_call_id for m in provider.requests[-1].messages for r in m.tool_results}
    assert calls == answered


def test_session_is_saved_and_resumable(agent_project: Path, make_session) -> None:
    session, _provider, _ui = make_session(
        agent_project, [reply("", [("list_files", {})]), reply("There are 4 files.")]
    )
    session.run_turn("how many files?")
    session.close()
    assert session.record is not None
    store = SessionStore(session.app.db, session.app.redactor)
    record = store.get(session.record.id)
    assert record.title == "how many files?"
    assert record.turns == 1 and record.status == "closed"
    assert record.usage.input_tokens == 200
    messages = store.messages(record.id)
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    assert store.latest(str(session.app.root)).id == record.id

    resumed, provider, _ = make_session(agent_project, [reply("Still 4.")])
    resumed.load_transcript(messages)
    assert resumed.turns == 1
    resumed.run_turn("and now?")
    assert len(provider.requests[0].messages) == 5


def test_undo_reverts_the_last_turn(agent_project: Path, make_session) -> None:
    calc = agent_project / "src" / "pyapp" / "calc.py"
    before = calc.read_text()
    session, _provider, _ = make_session(
        agent_project,
        [
            reply("", [("edit_file", {"path": "src/pyapp/calc.py", "old_text": "a + b", "new_text": "b + a"})]),
            reply("", [("write_file", {"path": "NEW.md", "content": "x"})]),
            reply("done"),
        ],
        mode=ApprovalMode.AUTO_EDIT,
    )
    session.run_turn("change things")
    assert calc.read_text() != before and (agent_project / "NEW.md").exists()
    assert sorted(session.undo()) == ["NEW.md", "src/pyapp/calc.py"]
    assert calc.read_text() == before and not (agent_project / "NEW.md").exists()
    assert session.undo() == []


def test_dry_run_shows_changes_without_writing(agent_project: Path, make_session) -> None:
    session, provider, _ = make_session(
        agent_project, [reply("", [("write_file", {"path": "a.txt", "content": "x"})]), reply("ok")], dry_run=True
    )
    session.run_turn("write")
    assert not (agent_project / "a.txt").exists()
    assert "[dry-run]" in provider.last_tool_results()[0].content


def test_non_interactive_changes_need_yes(agent_project: Path, make_session) -> None:
    session, provider, _ = make_session(
        agent_project, [reply("", [("write_file", {"path": "a.txt", "content": "x"})]), reply("ok")], interactive=False
    )
    session.run_turn("write")
    assert not (agent_project / "a.txt").exists()
    assert "no interactive terminal" in provider.last_tool_results()[0].content

    session, _provider, _ = make_session(
        agent_project,
        [reply("", [("write_file", {"path": "a.txt", "content": "x"})]), reply("ok")],
        interactive=False,
        yes=True,
    )
    session.run_turn("write")
    assert (agent_project / "a.txt").read_text() == "x"


def test_system_prompt_contains_project_context_and_memory(agent_project: Path, make_session) -> None:
    (agent_project / "HIGHHX.md").write_text("Always use tabs.\n")
    session, provider, _ = make_session(
        agent_project, [reply("", [("remember", {"fact": "Tests run with pytest -q"})]), reply("noted")]
    )
    session.run_turn("remember how tests run")
    system = provider.requests[0].system
    assert "Project: pyapp" in system and "Always use tabs." in system and "test: " in system
    assert session.memory.facts() == ["Tests run with pytest -q"]
    session.clear()
    assert "Tests run with pytest -q" in session.system
    assert sys.executable  # interpreter used by the fixture project exists
