from pathlib import Path

from highhx.config.schema import HighhXConfig
from highhx.policy.engine import PolicyEngine, PolicySet
from highhx.security.report import Finding, SecurityReport
from highhx.security.scanner import ScanContext, SecurityScanner
from highhx.security.secrets import Redactor, find_secrets, is_secret_key, mask, shannon_entropy

QUOTED_KEY = 'api_key = "q9X2kL7vR3mN8pZ4"'  # highhx:allow-secret
AWS_KEY = "AKIA" + "Z" * 12 + "7QRS"
GH_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def test_detects_known_secret_formats() -> None:
    text = f"aws = '{AWS_KEY}'\ntoken: {GH_TOKEN}\n-----BEGIN RSA PRIVATE KEY-----\n"  # highhx:allow-secret
    ids = {m.pattern_id for m in find_secrets(text)}
    assert {"aws-access-key-id", "github-token", "private-key"} <= ids


def test_generic_assignment_requires_entropy_and_skips_placeholders() -> None:
    assert find_secrets("password = 'changeme'") == []
    assert find_secrets("API_KEY=${API_KEY}") == []
    assert find_secrets("db_password = 'aaaaaaaaaaaa'") == []
    hits = find_secrets("db_password = 'Xk9#pQ2!vL7@mN4z'")  # highhx:allow-secret
    assert [h.pattern_id for h in hits] == ["generic-secret-assignment"]


def test_allowlist_comment() -> None:
    assert find_secrets(f"key = '{AWS_KEY}'  # highhx:allow-secret") == []


def test_url_credentials() -> None:
    hits = find_secrets("DATABASE_URL=postgres://app:SuperSecret99@db.internal:5432/app")  # highhx:allow-secret
    assert hits and hits[0].pattern_id == "url-credentials"


def test_redactor_masks_known_values_and_patterns() -> None:
    redactor = Redactor(["hunter2-very-secret"])
    text = redactor.redact(
        f"pw=hunter2-very-secret token={GH_TOKEN} url=postgres://u:pa55word@h/db"  # highhx:allow-secret
    )  # highhx:allow-secret
    assert "hunter2" not in text and GH_TOKEN not in text and "pa55word" not in text
    assert text.count("[REDACTED]") == 3


def test_redactor_ignores_paths_and_non_secret_keys(tmp_path: Path) -> None:
    redactor = Redactor()
    redactor.add_environment({"PWD": str(tmp_path), "SSH_AUTH_SOCK": "/tmp/x", "API_TOKEN": "abcd1234efgh"})
    assert redactor.redact(f"cwd {tmp_path} token abcd1234efgh") == f"cwd {tmp_path} token [REDACTED]"


def test_helpers() -> None:
    assert is_secret_key("STRIPE_SECRET_KEY") and not is_secret_key("PUBLIC_KEY_PATH") and not is_secret_key("PWD")
    assert mask("abc") == "******** (3 chars)" and mask("") == "(empty)"
    assert shannon_entropy("aaaa") == 0


def test_scanner_reports_findings_without_values(tmp_path: Path) -> None:
    (tmp_path / "config.py").write_text(f"KEY = '{AWS_KEY}'\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / ".env").chmod(0o644)
    config = HighhXConfig.from_dict({"approvals": {"auto_approve": "critical"}})
    ctx = ScanContext(
        root=tmp_path,
        config=config,
        policy=PolicyEngine(PolicySet(forbidden_files=[".env"])),
        tracked_files=lambda: ["config.py", ".env"],
        is_ignored=lambda _p: False,
    )
    report = SecurityScanner(ctx).run()
    rules = {f.rule for f in report.findings}
    assert "secret:aws-access-key-id" in rules
    assert "committed-sensitive-file" in rules
    assert "approvals-auto-dangerous" in rules
    assert "env-not-ignored" in rules
    assert AWS_KEY not in str(report.to_dict())
    assert "does not mean the project is secure" in report.to_markdown()


def test_allowlisted_fingerprints_are_suppressed(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(f"K = '{AWS_KEY}'\n")
    ctx = ScanContext(root=tmp_path, config=HighhXConfig(), policy=PolicyEngine(), tracked_files=lambda: ["a.py"])
    first = SecurityScanner(ctx).run(["secrets"])
    fingerprint = first.findings[0].fingerprint
    ctx.config = HighhXConfig.from_dict({"security": {"allowlist": [fingerprint]}})
    second = SecurityScanner(ctx).run(["secrets"])
    assert not second.findings and second.suppressed == 1


def test_report_thresholds() -> None:
    report = SecurityReport([Finding("r", "medium", "t", "c"), Finding("r2", "low", "t", "c")])
    assert len(report.at_least("medium")) == 1 and not report.at_least("high")


def test_source_code_is_not_mistaken_for_credentials() -> None:
    code = "token = self.next()\nsecret = manager.is_secret(key)\npassword=unquote(parsed.password)\ntoken: CancellationToken,\n"
    assert find_secrets(code, source_code=True) == []
    assert find_secrets("print('token=' + os.environ['GITHUB_TOKEN'])", source_code=True) == []
    assert (
        find_secrets(QUOTED_KEY, source_code=True)[0].pattern_id == "generic-secret-assignment"
    )  # highhx:allow-secret


def test_bare_config_values_must_look_random() -> None:
    assert find_secrets("API_KEY=q9X2kL7vR3mN8pZ4") and not find_secrets("SESSION_TOKEN=CancellationToken")
    assert find_secrets("see postgresql://user:pass@host:5432/db for the format") == []
