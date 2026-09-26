"""Policy-driven and configuration security checks."""

from __future__ import annotations

from pathlib import Path

from highhx.approvals.risk import RiskLevel, classify_command
from highhx.config.schema import HighhXConfig
from highhx.policy.engine import PolicyEngine
from highhx.security.report import Finding
from highhx.workflows.loader import WorkflowLoader


def committed_file_findings(tracked: list[str], policy: PolicyEngine) -> list[Finding]:
    findings = []
    patterns = policy.policies.forbidden_files or [".env", "*.pem", "*.key", "id_rsa", "id_ed25519"]
    engine = PolicyEngine(policy.policies) if policy.policies.forbidden_files else _default(patterns)
    for path in engine.forbidden_matches(tracked):
        name = path.rsplit("/", 1)[-1]
        if name in (".env.example", ".env.sample", ".env.template"):
            continue
        findings.append(
            Finding(
                "committed-sensitive-file",
                "high",
                "Sensitive file is committed to git",
                "policy",
                path,
                remediation=f"git rm --cached {path}, add it to .gitignore and rotate any secrets it contained",
            )
        )
    return findings


def _default(patterns: list[str]) -> PolicyEngine:
    from highhx.policy.engine import PolicySet

    return PolicyEngine(PolicySet(forbidden_files=patterns))


def gitignore_findings(root: Path, is_ignored: object | None) -> list[Finding]:
    findings: list[Finding] = []
    for env in sorted(root.glob(".env*")):
        if not env.is_file() or env.name in (".env.example", ".env.sample", ".env.template"):
            continue
        if callable(is_ignored) and not is_ignored(env.name):
            findings.append(
                Finding(
                    "env-not-ignored",
                    "medium",
                    f"{env.name} is not ignored by git",
                    "config",
                    env.name,
                    remediation=f"Add {env.name} to .gitignore",
                )
            )
    return findings


def workflow_findings(loader: WorkflowLoader) -> list[Finding]:
    findings = []
    for ref in loader.list():
        if ref.error:
            continue
        try:
            spec = loader.load(ref.key)
        except Exception:
            continue
        rel = ref.path.name
        for step in spec.steps:
            for command in step.run:
                classification = classify_command(command)
                if "pipe-to-shell" in classification.rule_ids:
                    findings.append(
                        Finding(
                            "workflow-pipe-to-shell",
                            "high",
                            f"Step '{step.id}' pipes a download into a shell",
                            "workflows",
                            f".highhx/workflows/{rel}",
                            detail=command[:120],
                            remediation="Download, verify (checksum/signature) and then execute.",
                        )
                    )
                elif classification.risk >= RiskLevel.DANGEROUS and step.approval is None:
                    findings.append(
                        Finding(
                            "workflow-unapproved-dangerous",
                            "medium" if classification.risk == RiskLevel.DANGEROUS else "high",
                            f"Step '{step.id}' runs a {classification.risk.label} command without `approval`",
                            "workflows",
                            f".highhx/workflows/{rel}",
                            detail="; ".join(classification.reasons),
                            remediation="Add `approval: true` to the step.",
                        )
                    )
    return findings


def config_findings(config: HighhXConfig) -> list[Finding]:
    findings = []
    approvals = config.approvals
    auto = str(approvals.get("auto_approve", "normal")).lower()
    if auto in ("dangerous", "critical"):
        findings.append(
            Finding(
                "approvals-auto-dangerous",
                "high",
                f"approvals.auto_approve is '{auto}'",
                "config",
                ".highhx/config.yaml",
                detail="Dangerous actions will run without confirmation.",
                remediation="Set approvals.auto_approve to normal.",
            )
        )
    if config.plugins.allow_code:
        findings.append(
            Finding(
                "plugins-allow-code",
                "low",
                "Plugins may execute Python code",
                "config",
                ".highhx/config.yaml",
                detail="plugins.allow_code is true; only install plugins you trust.",
                remediation="Review installed plugins with `highhx plugin list`.",
            )
        )
    if config.database.url and "@" in config.database.url:
        findings.append(
            Finding(
                "database-url-in-config",
                "high",
                "Database URL with credentials is stored in config",
                "config",
                ".highhx/config.yaml",
                remediation="Use database.url_env and an environment variable.",
            )
        )
    for target in config.deploy_targets.values():
        if target.production and not target.health_check:
            findings.append(
                Finding(
                    "prod-no-health-check",
                    "low",
                    f"Production target '{target.name}' has no health check",
                    "config",
                    ".highhx/config.yaml",
                    remediation="Add deploy.targets.<name>.health_check so failed deployments are detected.",
                )
            )
        hc = target.health_check
        if (
            target.production
            and hc
            and hc.url
            and hc.url.startswith("http://")
            and not any(h in hc.url for h in ("localhost", "127.0.0.1"))
        ):
            findings.append(
                Finding(
                    "prod-health-http",
                    "low",
                    f"Production target '{target.name}' health check uses plain HTTP",
                    "config",
                    ".highhx/config.yaml",
                    remediation="Use https://",
                )
            )
        for key in target.env:
            from highhx.security.secrets import is_secret_key

            if is_secret_key(key) and target.env[key] and not target.env[key].startswith("$"):
                findings.append(
                    Finding(
                        "secret-in-deploy-env",
                        "high",
                        f"Target '{target.name}' stores {key} in config",
                        "config",
                        ".highhx/config.yaml",
                        remediation="Reference an environment variable instead of a literal value.",
                    )
                )
    return findings


def env_profile_findings(values_by_profile: dict[str, dict[str, str]]) -> list[Finding]:
    findings = []
    production = values_by_profile.get("production") or {}
    for key in ("DEBUG", "APP_DEBUG", "FLASK_DEBUG", "DJANGO_DEBUG"):
        if production.get(key, "").lower() in ("1", "true", "yes", "on"):
            findings.append(
                Finding(
                    "prod-debug-enabled",
                    "medium",
                    f"{key} is enabled in the production profile",
                    "config",
                    ".env.production",
                    remediation=f"Set {key}=false for production.",
                )
            )
    return findings
