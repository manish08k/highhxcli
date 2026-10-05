"""Workflow blocks (this phase): loops (for_each), validation (verify), file parsing
(filesystem.parse) and e-mail (email.send). Every iteration and every block is an ordinary
action through the executor: classified, approved, audited."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.handlers import api
from highhx.core.result import Status
from highhx.workflows.validator import validate_data


def write_workflow(root: Path, name: str, body: str) -> None:
    (root / ".highhx" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / ".highhx" / "workflows" / f"{name}.yaml").write_text(body)


LOOP = """
name: invoices
steps:
  - id: read
    action: filesystem.parse
    with:
      path: invoices.csv
  - id: notes
    depends_on: read
    for_each: ${{ fromjson(steps.read.outputs.data) }}
    action: filesystem.write
    with:
      path: "out/${{ item.id }}.txt"
      content: "${{ loop.number }}/${{ loop.count }} ${{ item.customer }} owes ${{ item.amount }}\\n"
    verify:
      file:
        path: "out/${{ item.id }}.txt"
        contains: "${{ item.customer }}"
  - id: literal
    for_each: [a, b]
    action: filesystem.write
    with:
      path: "lit-${{ item }}.txt"
      content: "${{ item }}\\n"
"""


def test_a_loop_over_parsed_rows_with_verification(agent_project: Path, make_app) -> None:
    (agent_project / "invoices.csv").write_text("id,customer,amount\nA1,Ada,10\nB2,Bob,20\nC3,Cy,30\n")
    write_workflow(agent_project, "invoices", LOOP)
    app = make_app(agent_project, yes=True, interactive=False)
    result = app.workflows.run("invoices")
    assert result.status == Status.SUCCESS, {k: v.message for k, v in result.steps.items()}
    assert (agent_project / "out" / "B2.txt").read_text() == "2/3 Bob owes 20\n"
    assert sorted(p.name for p in (agent_project / "out").iterdir()) == ["A1.txt", "B2.txt", "C3.txt"]
    notes = result.steps["notes"]
    assert notes.outputs["count"] == "3" and len(json.loads(notes.outputs["results"])) == 3
    assert (agent_project / "lit-b.txt").read_text() == "b\n"
    # every iteration is its own audited action (3 notes + 2 literals), all through the executor
    from highhx.safety.audit import AuditLog

    writes = [e for e in AuditLog(app.db, app.redactor).list(limit=50) if e.tool == "filesystem.write"]
    assert len(writes) == 5 and all(e.status == "ok" for e in writes)


def test_a_failed_verification_fails_the_step_and_stops_the_loop(agent_project: Path, make_app) -> None:
    write_workflow(
        agent_project,
        "checked",
        """
name: checked
steps:
  - id: write
    for_each: [one, two, three]
    action: filesystem.write
    with:
      path: "${{ item }}.txt"
      content: "x\\n"
    verify:
      file: {path: "${{ item }}.txt", contains: "one"}
""",
    )
    app = make_app(agent_project, yes=True, interactive=False)
    result = app.workflows.run("checked")
    step = result.steps["write"]
    assert result.status == Status.FAILED and step.status == Status.FAILED
    assert step.message.startswith("item 1 of 3: verification")  # "x" does not contain "one"
    assert step.outputs["failed_index"] == "0"
    assert not (agent_project / "two.txt").exists()  # the loop stopped at the first failure


def test_verify_on_a_command_step(agent_project: Path, make_app) -> None:
    import sys

    write_workflow(
        agent_project,
        "cmd",
        f"""
name: cmd
steps:
  - id: make
    run: {sys.executable} -c "open('made.txt','w').write('done')"
    verify:
      all:
        - exit_code: 0
        - file: {{path: made.txt, contains: done}}
""",
    )
    app = make_app(agent_project, yes=True, interactive=False)
    assert app.workflows.run("cmd").status == Status.SUCCESS


def test_loop_validation() -> None:
    def errors(body: str) -> list[str]:
        import yaml

        return validate_data(yaml.safe_load(body), name="w", check_tools=False).errors

    assert any(
        "only available in a step with for_each" in e
        for e in errors("name: w\nsteps:\n  - id: a\n    action: filesystem.read\n    with: {path: '${{ item }}'}\n")
    )
    assert any(
        "cannot refer to item or loop" in e
        for e in errors(
            "name: w\nsteps:\n  - id: a\n    for_each: '${{ item }}'\n    action: filesystem.read\n    with: {path: x}\n"
        )
    )
    assert any(
        "at most 1000 items" in e
        for e in errors(
            "name: w\nsteps:\n  - id: a\n    for_each: " + json.dumps(list(range(1001))) + "\n    run: echo\n"
        )
    )
    assert any(
        "verify" in e for e in errors("name: w\nsteps:\n  - id: a\n    run: echo\n    verify: {no_such_check: 1}\n")
    )
    assert errors("name: w\nsteps:\n  - id: a\n    for_each: [1, 2]\n    run: echo ${{ item }}\n") == []


def test_a_loop_expression_that_is_not_a_list_fails_cleanly(agent_project: Path, make_app) -> None:
    write_workflow(
        agent_project,
        "bad",
        "name: bad\nsteps:\n  - id: a\n    for_each: '${{ workflow.name }}'\n    action: filesystem.read\n    with: {path: x}\n",
    )
    app = make_app(agent_project, yes=True, interactive=False)
    result = app.workflows.run("bad")
    assert result.steps["a"].status == Status.FAILED and "for_each" in result.steps["a"].message


# ------------------------------------------------------------------- parsing
def test_parsing_files_as_data(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    (agent_project / "data.json").write_text('{"items": [1, 2]}')
    (agent_project / "log.jsonl").write_text('{"a": 1}\n\n{"a": 2}\n')
    (agent_project / "conf.yaml").write_text("name: x\nlist: [1, 2]\n")
    (agent_project / "rows.tsv").write_text("k\tv\nx\t1\n")
    (agent_project / "broken.json").write_text("{nope")
    assert executor.run("filesystem.parse", {"path": "data.json"}).output["data"] == {"items": [1, 2]}
    assert executor.run("filesystem.parse", {"path": "log.jsonl"}).output["data"] == [{"a": 1}, {"a": 2}]
    assert executor.run("filesystem.parse", {"path": "conf.yaml"}).output["data"] == {"name": "x", "list": [1, 2]}
    tsv = executor.run("filesystem.parse", {"path": "rows.tsv"}).output
    assert tsv["data"] == [{"k": "x", "v": "1"}] and tsv["columns"] == ["k", "v"]
    broken = executor.run("filesystem.parse", {"path": "broken.json"})
    assert not broken.ok and "not valid json" in broken.error
    assert executor.plan("filesystem.parse", {"path": "data.json"}).decision.risk == 0  # a read


def test_parsing_refuses_secrets_and_escapes(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    (agent_project / ".env").write_text("TOKEN=abc\n")
    secret = executor.run("filesystem.parse", {"path": ".env", "format": "text"})
    assert not secret.ok and "abc" not in json.dumps(secret.output)
    outside = executor.run("filesystem.parse", {"path": "/etc/hosts", "format": "text"})
    assert not outside.ok


def test_yaml_is_parsed_safely(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    (agent_project / "evil.yaml").write_text("!!python/object/apply:os.system ['echo pwned > pwned.txt']\n")
    result = executor.run("filesystem.parse", {"path": "evil.yaml"})
    assert not result.ok and not (agent_project / "pwned.txt").exists()


# --------------------------------------------------------------------- email
SMTP_SERVERS: list[Any] = []


@pytest.fixture(autouse=True)
def close_smtp_servers() -> Any:
    yield
    while SMTP_SERVERS:
        SMTP_SERVERS.pop().server.close()


class FakeSMTP:
    """A minimal SMTP server on 127.0.0.1 (no TLS): records the envelope and message."""

    def __init__(self, starttls: bool = False) -> None:
        SMTP_SERVERS.append(self)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.starttls = starttls
        self.lines: list[str] = []
        self.data = ""
        self.recipients: list[str] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            conn, _ = self.server.accept()
        except OSError:  # closed by the test before anyone connected
            return
        f = conn.makefile("rwb")

        def say(text: str) -> None:
            f.write(text.encode() + b"\r\n")
            f.flush()

        say("220 fake ESMTP")
        while True:
            line = f.readline().decode().rstrip("\r\n")
            if not line:
                break
            self.lines.append(line)
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                say("250-fake" + ("\r\n250-STARTTLS" if self.starttls else "") + "\r\n250 AUTH PLAIN")
            elif verb == "AUTH":
                say("235 ok")
            elif verb == "MAIL":
                say("250 ok")
            elif verb == "RCPT":
                address = line.split(":", 1)[1].strip(" <>")
                if address.startswith("nobody@"):
                    say("550 no such user")
                else:
                    self.recipients.append(address)
                    say("250 ok")
            elif verb == "DATA":
                say("354 go")
                body = []
                while (chunk := f.readline().decode()) not in (".\r\n", ""):
                    body.append(chunk)
                self.data = "".join(body)
                say("250 queued")
            elif verb == "QUIT":
                say("221 bye")
                break
            else:
                say("502 no")
        f.close()
        conn.close()


@pytest.fixture
def smtp_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    def configure(server: FakeSMTP) -> None:
        monkeypatch.setenv("HIGHHX_SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("HIGHHX_SMTP_PORT", str(server.port))
        monkeypatch.setenv("HIGHHX_SMTP_USER", "bot@example.com")
        monkeypatch.setenv("HIGHHX_SMTP_PASSWORD", "smtp-PASSWORD-1")
        monkeypatch.setenv("HIGHHX_SMTP_FROM", "bot@example.com")

    return configure


def test_email_is_asked_first_and_sent(agent_project: Path, executor_for, smtp_env) -> None:
    server = FakeSMTP()
    smtp_env(server)
    executor, ui = executor_for(agent_project)
    events: list[Any] = []
    executor.events.subscribe("*", events.append)
    result = executor.run(
        "email.send",
        {"to": ["ada@example.com", "nobody@example.com"], "subject": "Report", "body": "The body text BODY-SECRET"},
    )
    assert ui.requests, "a high-risk action is always asked"
    assert server.recipients == ["ada@example.com"] and "BODY-SECRET" in server.data
    assert not result.ok and result.output["refused"] == ["nobody@example.com"]  # partial delivery is not success
    shown = json.dumps([getattr(e, "data", getattr(e, "payload", {})) for e in events], default=str)
    assert "smtp-PASSWORD-1" not in shown and "BODY-SECRET" not in shown  # the body is shown as its length


def test_email_declined_sends_nothing(agent_project: Path, executor_for, smtp_env) -> None:
    server = FakeSMTP()
    smtp_env(server)
    executor, ui = executor_for(agent_project)
    ui.action_answers = [False]
    assert executor.run("email.send", {"to": ["ada@example.com"], "subject": "x", "body": "y"}).status == "denied"
    assert server.recipients == [] and server.data == ""


def test_email_refuses_clear_text_to_a_remote_server(agent_project: Path, executor_for, smtp_env, monkeypatch) -> None:
    server = FakeSMTP(starttls=False)
    smtp_env(server)
    monkeypatch.setattr(api, "is_loopback", lambda url: False)  # pretend the server is elsewhere
    executor, _ = executor_for(agent_project)
    result = executor.run("email.send", {"to": ["ada@example.com"], "subject": "x", "body": "y"})
    assert not result.ok and "TLS" in result.error
    assert not any(line.upper().startswith(("AUTH", "MAIL", "DATA")) for line in server.lines)  # nothing sent


def test_email_without_configuration_or_with_injection_fails_before_sending(
    agent_project: Path, executor_for, monkeypatch
) -> None:
    for name in api.SMTP_ENV.values():
        monkeypatch.delenv(name, raising=False)
    executor, _ = executor_for(agent_project)
    unconfigured = executor.run("email.send", {"to": ["ada@example.com"], "subject": "x", "body": "y"})
    assert not unconfigured.ok and "HIGHHX_SMTP_HOST" in unconfigured.error
    from highhx.core.errors import ValidationError

    with pytest.raises(ValidationError):
        executor.plan("email.send", {"to": ["ada@example.com\r\nBcc: all@example.com"], "subject": "x", "body": "y"})
    with pytest.raises(ValidationError):
        executor.plan("email.send", {"to": ["ada@example.com"], "subject": "x\r\nBcc: all@example.com", "body": "y"})


def test_runtime_loop_items_in_a_command_line_are_flagged_and_still_classified(agent_project: Path, make_app) -> None:
    """A CSV row like ``x; rm -rf ~`` interpolated into ``run:`` is shell injection: the validator
    warns, and the resulting command is still classified (and asked) like any other."""
    import yaml

    body = """
name: risky
steps:
  - id: read
    action: filesystem.parse
    with: {path: names.csv}
  - id: greet
    depends_on: read
    for_each: ${{ fromjson(steps.read.outputs.data) }}
    run: echo ${{ item.name }}
"""
    warnings = validate_data(yaml.safe_load(body), name="risky", check_tools=False).warnings
    assert any("loop items computed at run time" in w for w in warnings)
    literal = {"name": "ok", "steps": [{"id": "a", "for_each": ["x", "y"], "run": "echo ${{ item }}"}]}
    assert not any("loop items" in w for w in validate_data(literal, name="ok", check_tools=False).warnings)
    (agent_project / "names.csv").write_text("name\nAda\nx; rm -rf ~/highhx-test-canary\n")
    canary = Path.home() / "highhx-test-canary"
    assert not canary.exists()
    write_workflow(agent_project, "risky", body)
    app = make_app(agent_project, interactive=False)  # not --yes: anything risky must be asked, and nobody answers
    result = app.workflows.run("risky")
    greet = result.steps["greet"]
    assert greet.status != Status.SUCCESS and greet.message.startswith("item 2 of 2"), (
        greet.message
    )  # row 1 ran; the injected row was refused
    assert not canary.exists()


def _minimal_pdf(text: str) -> bytes:
    """A one-page PDF with one line of text (no dependency needed to make it)."""
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


@pytest.mark.skipif(not __import__("shutil").which("pdftotext"), reason="pdftotext (poppler) is not installed")
def test_parsing_a_pdf_with_pdftotext(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    (agent_project / "invoice.pdf").write_bytes(_minimal_pdf("Invoice A-1007 total 42"))
    result = executor.run("filesystem.parse", {"path": "invoice.pdf"})
    assert result.ok and result.output["format"] == "document" and "Invoice A-1007 total 42" in result.output["text"]


def test_parsing_a_pdf_without_pdftotext_says_what_to_install(agent_project: Path, executor_for, monkeypatch) -> None:
    import shutil

    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "pdftotext" else real(name))
    executor, _ = executor_for(agent_project)
    (agent_project / "invoice.pdf").write_bytes(_minimal_pdf("x"))
    result = executor.run("filesystem.parse", {"path": "invoice.pdf"})
    assert not result.ok and "pdftotext" in result.error


class MultiSMTP(FakeSMTP):
    """FakeSMTP that serves several connections; the first ``busy`` are turned away with 421."""

    def __init__(self, busy: int = 0) -> None:
        self.busy = busy
        self.connections = 0
        super().__init__()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            if self.connections <= self.busy:
                conn.sendall(b"421 too busy, try later\r\n")
                conn.close()
                continue
            self.server_conn = conn
            self._session(conn)

    def _session(self, conn: socket.socket) -> None:
        f = conn.makefile("rwb")

        def say(text: str) -> None:
            f.write(text.encode() + b"\r\n")
            f.flush()

        say("220 fake ESMTP")
        while True:
            line = f.readline().decode().rstrip("\r\n")
            if not line:
                break
            self.lines.append(line)
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                say("250-fake\r\n250 AUTH PLAIN")
            elif verb in ("AUTH", "MAIL"):
                say("235 ok" if verb == "AUTH" else "250 ok")
            elif verb == "RCPT":
                self.recipients.append(line.split(":", 1)[1].strip(" <>"))
                say("250 ok")
            elif verb == "DATA":
                say("354 go")
                body = []
                while (chunk := f.readline().decode()) not in (".\r\n", ""):
                    body.append(chunk)
                self.data = "".join(body)
                say("250 queued")
            elif verb == "QUIT":
                say("221 bye")
                break
        conn.close()


def test_email_with_html_attachments_and_a_reply(agent_project: Path, executor_for, smtp_env) -> None:
    import email

    server = MultiSMTP()
    smtp_env(server)
    (agent_project / "report.csv").write_text("a,b\n1,2\n")
    executor, _ = executor_for(agent_project)
    events: list[Any] = []
    executor.events.subscribe("*", events.append)
    result = executor.run(
        "email.send",
        {
            "to": ["ada@example.com"],
            "subject": "Re: report",
            "body": "see attached",
            "html": "<p>see <b>attached</b> HTML-SECRET</p>",
            "attachments": ["report.csv"],
            "in_reply_to": "<orig-1@example.com>",
        },
    )
    assert result.ok and result.output["attachments"] == [{"name": "report.csv", "bytes": 8, "type": "text/csv"}]
    parsed = email.message_from_string(server.data)
    assert parsed["In-Reply-To"] == "<orig-1@example.com>" and parsed["References"] == "<orig-1@example.com>"
    parts = {part.get_content_type(): part for part in parsed.walk()}
    assert "text/plain" in parts and "text/html" in parts and parts["text/csv"].get_filename() == "report.csv"
    assert parts["text/csv"].get_payload(decode=True) == b"a,b\n1,2\n"
    shown = json.dumps([getattr(e, "data", {}) for e in events], default=str)
    assert "HTML-SECRET" not in shown  # the HTML body is shown as its length, like the text


def test_email_attachments_never_include_secret_or_outside_files(agent_project: Path, executor_for, smtp_env) -> None:
    server = MultiSMTP()
    smtp_env(server)
    (agent_project / ".env").write_text("TOKEN=abc\n")
    executor, _ = executor_for(agent_project)
    for path in (".env", "/etc/hosts", "../outside.txt"):
        result = executor.run(
            "email.send", {"to": ["ada@example.com"], "subject": "x", "body": "y", "attachments": [path]}
        )
        assert not result.ok, path
    assert server.data == ""  # nothing was sent
    bad_reply = executor.run(
        "email.send", {"to": ["ada@example.com"], "subject": "x", "body": "y", "in_reply_to": "not-an-id"}
    )
    assert not bad_reply.ok and "Message-ID" in bad_reply.error and server.data == ""


def test_email_retries_only_before_the_message_is_handed_over(agent_project: Path, executor_for, smtp_env) -> None:
    server = MultiSMTP(busy=2)
    smtp_env(server)
    executor, _ = executor_for(agent_project)
    result = executor.run("email.send", {"to": ["ada@example.com"], "subject": "x", "body": "y", "retries": 2})
    assert (
        result.ok
        and result.output["attempts"] == 3
        and server.connections == 3
        and server.recipients == ["ada@example.com"]
    )
