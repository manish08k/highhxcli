"""Agent tools and the permission layer."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from highhx.agent.permissions import AgentPermissions, ApprovalMode
from highhx.agent.tools.base import ToolContext, ToolError, truncate
from highhx.agent.tools.registry import ToolRegistry, builtin_tools
from highhx.cloud.plans import AGENT, AGENT_DEPLOY, CLI, PLANS, PRO
from highhx.core.errors import ApprovalDeniedError
from tests.conftest import init_repo
from tests.unit.agent.conftest import RecordingUI, reply


def run_tool(session, name: str, args: dict):
    tool = session.registry.get(name)
    assert tool is not None, name
    problems = tool.validate(args)
    assert problems == [], problems
    return tool.run(session._tool_context(), args)


@pytest.fixture
def session(agent_project: Path, make_session):
    s, _provider, _ui = make_session(agent_project, [], mode=ApprovalMode.AUTO_EDIT)
    return s


# -------------------------------------------------------------------- registry
def test_every_builtin_tool_has_a_valid_schema() -> None:
    registry = ToolRegistry(builtin_tools())
    for spec in registry.specs():
        assert spec.name and spec.description, spec.name
        assert spec.parameters["type"] == "object"
        assert spec.parameters.get("additionalProperties") is False


def test_registry_respects_plan_features() -> None:
    limited = ToolRegistry.for_session(features=frozenset({CLI, AGENT}))
    assert "read_file" in limited and "edit_file" not in limited and "deploy" not in limited
    full = ToolRegistry.for_session(features=PLANS[PRO].features)
    assert {"edit_file", "run_command", "git_commit", "deploy"} <= set(full.names())
    no_deploy = ToolRegistry.for_session(features=PLANS[PRO].features - {AGENT_DEPLOY})
    assert "deploy" not in no_deploy and "rollback" not in no_deploy
    assert "run_workflow" not in ToolRegistry.for_session(features=PLANS[PRO].features, initialized=False)


def test_duplicate_tool_names_are_rejected() -> None:
    tools = builtin_tools()
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry([*tools, tools[0]])


def test_truncate_keeps_head_and_tail() -> None:
    text = "".join(f"line {i}\n" for i in range(10_000))
    out = truncate(text, 1000)
    assert out.startswith("line 0") and out.rstrip().endswith("line 9999") and "omitted" in out


# ------------------------------------------------------------------ permissions
def test_paths_are_confined_to_the_project(session) -> None:
    perms: AgentPermissions = session.permissions
    for bad in ("../outside.txt", "/etc/passwd", "src/../../x"):
        with pytest.raises(ToolError, match="outside the project"):
            perms.resolve(bad)
    assert perms.resolve("src/pyapp/calc.py").name == "calc.py"


def test_symlink_escapes_are_rejected(session, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (session.app.root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolError, match="outside the project"):
        session.permissions.resolve("link/file.txt", write=True)


@pytest.mark.parametrize("name", [".env", ".env.production", "id_rsa", "server.pem", "secrets.yaml", "db_password.txt"])
def test_secret_files_are_never_accessible(session, name: str) -> None:
    (session.app.root / name).write_text("SECRET=1\n")
    with pytest.raises(ToolError, match="secrets"):
        session.permissions.resolve(name)


@pytest.mark.parametrize("name", [".env.example", "secrets.py", "auth_token_service.ts", "tokenizer.json", "AUTHORS"])
def test_ordinary_files_are_not_treated_as_secrets(name: str) -> None:
    assert not AgentPermissions.is_secret(name)


def test_state_and_vcs_directories_are_protected(session) -> None:
    with pytest.raises(ToolError, match="internal state"):
        session.permissions.resolve(".highhx/state/highhx.db")
    with pytest.raises(ToolError, match="protected"):
        session.permissions.resolve(".git/config", write=True)


def test_forbidden_files_policy_applies_to_writes(agent_project: Path, make_session) -> None:
    (agent_project / ".highhx" / "policies.yaml").write_text("forbidden_files:\n  - '*.sqlite'\n")
    s, _p, _u = make_session(agent_project, [], mode=ApprovalMode.AUTO_EDIT)
    with pytest.raises(ToolError, match="forbidden_files"):
        s.permissions.resolve("data/app.sqlite", write=True)


def test_read_only_mode_refuses_changes(session) -> None:
    from highhx.safety.actions import ActionKind

    session.permissions.mode = ApprovalMode.READ_ONLY
    action = session.permissions.action(ActionKind.WRITE_FILE, "Edit x", tool="edit_file", target="x")
    with pytest.raises(ApprovalDeniedError, match="read-only"):
        session.permissions.authorize(action, policy_action="agent:write")


def test_always_confirm_asks_even_in_auto_edit(session) -> None:
    from highhx.safety.actions import ActionKind

    ui: RecordingUI = session.ui
    ui.action_answers.append(False)
    action = session.permissions.action(ActionKind.DEPLOY, "Deploy to staging", tool="deploy", target="staging")
    with pytest.raises(ApprovalDeniedError):
        session.permissions.authorize(action, policy_action="agent:deploy:staging", always_confirm=True)
    assert ui.of("confirm_action") == ["Deploy to staging"]


# ------------------------------------------------------------------ read tools
def test_read_file_numbers_lines_and_windows(session) -> None:
    path = session.app.root / "long.txt"
    path.write_text("".join(f"row {i}\n" for i in range(1, 51)))
    result = run_tool(session, "read_file", {"path": "long.txt", "offset": 10, "limit": 3})
    assert "10  row 10" in result.content and "12  row 12" in result.content and "row 13" not in result.content
    assert "offset=13" in result.content


def test_read_file_rejects_binary_and_directories(session) -> None:
    (session.app.root / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    with pytest.raises(ToolError, match="binary"):
        run_tool(session, "read_file", {"path": "blob.bin"})
    with pytest.raises(ToolError, match="directory"):
        run_tool(session, "read_file", {"path": "src"})


def test_list_files_skips_noise_and_secrets(session) -> None:
    root = session.app.root
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("x")
    (root / ".env").write_text("A=1")
    result = run_tool(session, "list_files", {"glob": "*.py"})
    assert "src/pyapp/calc.py" in result.content and "tests/test_calc.py" in result.content
    everything = run_tool(session, "list_files", {})
    assert "node_modules" not in everything.content and ".env" not in everything.content.split()


def test_search_code_finds_matches(session) -> None:
    result = run_tool(session, "search_code", {"pattern": r"def add\(", "glob": "*.py"})
    assert "src/pyapp/calc.py:1: def add(a: int, b: int) -> int:" in result.content
    with pytest.raises(ToolError, match="invalid regular expression"):
        run_tool(session, "search_code", {"pattern": "("})


def test_project_overview_reports_commands(session) -> None:
    result = run_tool(session, "project_overview", {})
    assert '"test":' in result.content and "pyapp" in result.summary


# ----------------------------------------------------------------- edit tools
def test_edit_file_requires_unique_exact_match(session) -> None:
    path = session.app.root / "dup.py"
    path.write_text("x = 1\nx = 1\n")
    with pytest.raises(ToolError, match="occurs 2 times"):
        run_tool(session, "edit_file", {"path": "dup.py", "old_text": "x = 1", "new_text": "x = 2"})
    with pytest.raises(ToolError, match="not found"):
        run_tool(session, "edit_file", {"path": "dup.py", "old_text": "y = 1", "new_text": "y = 2"})
    run_tool(session, "edit_file", {"path": "dup.py", "old_text": "x = 1", "new_text": "x = 2", "replace_all": True})
    assert path.read_text() == "x = 2\nx = 2\n"


def test_edit_file_hints_at_whitespace_mismatch(session) -> None:
    (session.app.root / "w.py").write_text("def f():\n    return 1\n")
    with pytest.raises(ToolError, match="indentation"):
        run_tool(session, "edit_file", {"path": "w.py", "old_text": "  return 1  ", "new_text": "return 2"})


def test_write_and_delete_with_diff_details(agent_project: Path, make_session) -> None:
    ui = RecordingUI()
    s, _p, _u = make_session(agent_project, [], ui=ui)
    result = run_tool(s, "write_file", {"path": "docs/new.md", "content": "hello\n"})
    assert result.ok and result.changed_files == ["docs/new.md"] and "+1" in result.summary
    assert "--- /dev/null" in result.data["diff"]
    run_tool(s, "delete_file", {"path": "docs/new.md"})
    assert not (agent_project / "docs" / "new.md").exists()
    assert any(a.startswith("Delete docs/new.md") for a in ui.of("confirm_action"))


# -------------------------------------------------------------- command tools
def test_run_checks_reports_each_check(session) -> None:
    result = run_tool(session, "run_checks", {})
    assert result.ok, result.content
    assert "## lint" in result.content and "lint ok" in result.content and "## test" in result.content


def test_run_command_reports_exit_codes(session) -> None:
    import sys

    result = run_tool(session, "run_command", {"command": f'{sys.executable} -c "import sys; print(42); sys.exit(3)"'})
    assert not result.ok and "exit code 3" in result.content and "42" in result.content


def test_security_scan_reports_findings(session) -> None:
    fake_key = "AKIAIOSFODNN7EXAMPLF"  # highhx:allow-secret (test fixture)
    (session.app.root / "config.py").write_text(f'AWS_KEY = "{fake_key}"\n')
    result = run_tool(session, "security_scan", {"checks": ["secrets"]})
    assert "aws-access-key-id" in result.content and fake_key not in result.content
    assert result.summary.startswith("Security:")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_tools(agent_project: Path, make_session) -> None:
    (agent_project / ".gitignore").write_text(".highhx/state/\n.highhx/logs/\n")  # as `highhx init` writes
    init_repo(agent_project)
    s, _p, _u = make_session(agent_project, [], mode=ApprovalMode.AUTO_EDIT)
    (agent_project / "src" / "pyapp" / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    status = run_tool(s, "git_status", {})
    assert "1 changed" in status.summary
    diff = run_tool(s, "git_diff", {})
    assert "+    return b + a" in diff.content
    run_tool(s, "git_branch", {"name": "fix/add"})
    commit = run_tool(s, "git_commit", {"message": "fix: reorder operands", "all": True})
    assert commit.summary.startswith("commit ")
    log = run_tool(s, "git_log", {"limit": 5})
    assert "fix: reorder operands" in log.content
    with pytest.raises(ToolError, match="invalid revision"):
        run_tool(s, "git_diff", {"revision": "--output=/tmp/x"})


def test_deploy_requires_explicit_confirmation(agent_project: Path, make_session) -> None:
    (agent_project / ".highhx" / "config.yaml").write_text(
        (agent_project / ".highhx" / "config.yaml").read_text()
        + "deploy:\n  targets:\n    staging:\n      type: local\n      command: echo deployed\n"
    )
    ui = RecordingUI(action_answers=[False])
    s, provider, _ = make_session(
        agent_project,
        [reply("", [("deploy", {"target": "staging"})]), reply("Not deployed.")],
        ui=ui,
        mode=ApprovalMode.AUTO_EDIT,
    )
    s.run_turn("deploy this")
    assert ui.of("confirm_action") == ["Deploy application to staging"]
    result = provider.last_tool_results()[0]
    assert result.is_error and "Cancelled" in result.content
    assert s.app.deployments.history("staging") == []


def test_tool_context_is_fresh_per_call(session) -> None:
    ctx: ToolContext = session._tool_context()
    ctx.output_lines.append("x")
    assert session._tool_context().output_lines == []


def test_edits_invalidate_stale_python_bytecode(session) -> None:
    import py_compile

    from highhx.agent.tools.files import invalidate_bytecode

    source = session.app.root / "src" / "pyapp" / "calc.py"
    compiled = Path(py_compile.compile(str(source)))
    assert compiled.exists()
    run_tool(session, "edit_file", {"path": "src/pyapp/calc.py", "old_text": "a + b", "new_text": "b + a"})
    assert not compiled.exists()
    invalidate_bytecode(session.app.root / "README.md")  # non-Python files are ignored
