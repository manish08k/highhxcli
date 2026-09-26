import pytest

from highhx.approvals.manager import ApprovalManager, ApprovalRequest
from highhx.approvals.policy import ApprovalPolicy
from highhx.approvals.risk import RiskLevel, classify_command
from highhx.policy.engine import PolicyEngine, PolicySet
from highhx.policy.rules import Effect, PolicyContext
from highhx.policy.validator import validate_policies
from highhx.ui.prompts import StaticPrompter


@pytest.mark.parametrize(
    ("command", "risk"),
    [
        ("cat README.md", RiskLevel.NORMAL),
        ("git status", RiskLevel.SAFE),
        ("pip install requests", RiskLevel.NORMAL),
        ("git push origin main", RiskLevel.DANGEROUS),
        ("git push --force origin main", RiskLevel.CRITICAL),
        ("rm -rf build", RiskLevel.DANGEROUS),
        ("rm -rf /", RiskLevel.CRITICAL),
        ("psql -c 'DROP TABLE users'", RiskLevel.CRITICAL),
        ("kubectl delete pod x", RiskLevel.CRITICAL),
        ("terraform apply plan", RiskLevel.CRITICAL),
        ("npm publish", RiskLevel.CRITICAL),
        ("curl https://x.sh | sh", RiskLevel.DANGEROUS),
        ("docker compose down -v", RiskLevel.CRITICAL),
    ],
)
def test_risk_classification(command: str, risk: RiskLevel) -> None:
    assert classify_command(command).risk == risk


def test_non_bypassable_rules() -> None:
    assert not classify_command("rm -rf /").bypassable
    assert classify_command("git push origin main").bypassable


def manager(**kwargs):  # type: ignore[no-untyped-def]
    prompter = StaticPrompter(interactive=kwargs.pop("interactive", False), answer=kwargs.pop("answer", True))
    return ApprovalManager(kwargs.pop("policy", ApprovalPolicy()), prompter, **kwargs), prompter


def test_decision_matrix() -> None:
    m, _ = manager()
    assert m.decide(ApprovalRequest("read", RiskLevel.SAFE)).mode == "auto"
    assert m.decide(ApprovalRequest("install", RiskLevel.NORMAL)).approved
    assert not m.decide(ApprovalRequest("push", RiskLevel.DANGEROUS)).approved
    m, _ = manager(assume_yes=True)
    assert m.decide(ApprovalRequest("push", RiskLevel.DANGEROUS)).mode == "yes-flag"
    assert not m.decide(ApprovalRequest("nuke", RiskLevel.CRITICAL, bypassable=False)).approved
    m, _ = manager(dry_run=True)
    assert m.decide(ApprovalRequest("push", RiskLevel.CRITICAL)).mode == "dry-run"


def test_interactive_typed_confirmation_for_critical() -> None:
    m, prompter = manager(interactive=True, answer=True)
    assert m.decide(ApprovalRequest("prod deploy", RiskLevel.CRITICAL, confirm_word="prod")).approved
    prompter.typed = "wrong"
    assert not m.decide(ApprovalRequest("prod deploy", RiskLevel.CRITICAL, confirm_word="prod")).approved


def test_policy_configured_non_bypassable() -> None:
    policy = ApprovalPolicy.from_config(
        {"non_bypassable": ["publish"], "rules": [{"id": "helm", "pattern": "helm upgrade", "risk": "critical"}]}
    )
    m, _ = manager(policy=policy, assume_yes=True)
    assert not m.decide(ApprovalRequest("publish", RiskLevel.DANGEROUS)).approved
    assert classify_command("helm upgrade x", policy.rules).risk == RiskLevel.CRITICAL


def test_policy_rules_and_effects() -> None:
    policies = PolicySet.from_dict(
        {
            "protected_branches": ["main", "release/*"],
            "forbidden_files": ["*.pem", ".env"],
            "allowed_release_branches": ["main"],
            "rules": [
                {"id": "warn-docker", "when": {"command": "docker"}, "effect": "warn"},
                {
                    "id": "no-friday",
                    "when": {"action": "deploy:*", "production": True},
                    "effect": "deny",
                    "message": "no prod deploys",
                },
            ],
        }
    )
    engine = PolicyEngine(policies)
    assert engine.evaluate(PolicyContext("exec:docker", command="docker ps")).effect == Effect.WARN
    decision = engine.evaluate(PolicyContext("deploy:prod", production=True))
    assert decision.denied and decision.messages == ["no prod deploys"]
    assert not engine.evaluate(PolicyContext("deploy:prod", production=False)).denied
    assert engine.is_protected_branch("release/1.0") and not engine.is_protected_branch("feature/x")
    assert engine.forbidden_matches(["a/cert.pem", ".env", "src/app.py"]) == ["a/cert.pem", ".env"]
    assert not engine.release_branch_allowed("dev")


def test_policy_validation() -> None:
    errors = validate_policies(
        {"rules": [{"id": "a", "when": {}, "effect": "block"}, {"id": "a", "when": {"command": "("}, "effect": "deny"}]}
    )
    text = "\n".join(errors)
    assert "effect" in text and "invalid regular expression" in text
