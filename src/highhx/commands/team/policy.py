"""highhx policy"""

from __future__ import annotations

from typing import Any

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.core.result import CheckResult, CheckStatus


@click.group(
    "policy",
    cls=DefaultGroup,
    default_command="show",
    short_help="Project policies (protected branches, forbidden files, rules).",
)
def policy() -> None:
    """Policies live in .highhx/policies.yaml. Rules can allow, warn, require
    approval (optionally non-bypassable) or deny actions and commands."""


@policy.command("show", short_help="List policies and rules.")
@pass_app
def show(app: App) -> int:
    """Show protected branches, clean-tree requirements, forbidden files, release branches and
    every policy rule with its effect."""
    ps = app.policy.policies
    rules: list[dict[str, Any]] = [
        {
            "id": r.id,
            "effect": str(r.effect),
            "risk": r.risk.label,
            "bypassable": r.bypassable,
            "description": r.text(),
            "action": r.action,
            "command": r.command,
            "branch": r.branch,
            "target": r.target,
            "production": r.production,
        }
        for r in ps.rules
    ]
    data = {
        "source": str(ps.source) if ps.source else None,
        "protected_branches": ps.protected_branches,
        "require_clean_tree": ps.require_clean_tree,
        "forbidden_files": ps.forbidden_files,
        "allowed_release_branches": ps.allowed_release_branches,
        "rules": rules,
    }
    out = app.output

    def render() -> None:
        out.kv(
            {
                "protected branches": ps.protected_branches,
                "clean tree required for": ps.require_clean_tree or "-",
                "forbidden files": ps.forbidden_files or "-",
                "release branches": ps.allowed_release_branches or "any",
            },
            title="Policies",
        )
        if ps.rules:
            out.table(
                ["rule", "effect", "risk", "bypassable", "when", "description"],
                [
                    (
                        r["id"],
                        r["effect"],
                        r["risk"],
                        "yes" if r["bypassable"] else "NO",
                        ", ".join(
                            f"{k}={r[k]}"
                            for k in ("action", "command", "branch", "target", "production")
                            if r[k] is not None
                        ),
                        r["description"],
                    )
                    for r in rules
                ],
            )

    out.emit(data, render)
    return 0


@policy.command("check", short_help="Evaluate policies against the current repository state.")
@pass_app
def check(app: App) -> int:
    """Evaluate the policies against the repository now: forbidden files that are committed,
    whether the branch is protected, and clean-tree requirements. Exits 7 on violations."""
    engine = app.policy
    checks: list[CheckResult] = []
    repo = app.git_repo
    if repo.is_repo():
        forbidden = engine.forbidden_matches(repo.tracked_files())
        checks.append(
            CheckResult(
                "forbidden files committed",
                CheckStatus.FAIL if forbidden else CheckStatus.OK,
                ", ".join(forbidden) or "none",
                hint="git rm --cached <file> and rotate secrets" if forbidden else None,
            )
        )
        branch = repo.current_branch()
        checks.append(
            CheckResult(
                f"branch '{branch}'",
                CheckStatus.WARN if engine.is_protected_branch(branch) else CheckStatus.OK,
                "protected: pushes need approval" if engine.is_protected_branch(branch) else "not protected",
            )
        )
        clean = repo.status().clean
        for action in engine.policies.require_clean_tree:
            checks.append(
                CheckResult(
                    f"{action} requires a clean tree",
                    CheckStatus.OK if clean else CheckStatus.WARN,
                    "clean" if clean else "working tree has changes",
                )
            )
        if not engine.release_branch_allowed(branch):
            checks.append(CheckResult("release branch", CheckStatus.WARN, f"releases are not allowed from '{branch}'"))
    else:
        checks.append(CheckResult("git repository", CheckStatus.SKIP, "not a git repository"))
    failed = any(c.status == CheckStatus.FAIL for c in checks)
    app.output.emit({"ok": not failed, "checks": [c.to_dict() for c in checks]}, lambda: app.output.checks(checks))
    return 7 if failed else 0


@policy.command("validate", short_help="Validate policies.yaml.")
@pass_app
def validate(app: App) -> int:
    """Validate .highhx/policies.yaml and report every problem (exit code 3 when invalid)."""
    from highhx.config.loader import load_yaml
    from highhx.policy.validator import validate_policies

    path = app.paths.policies_file
    errors = validate_policies(load_yaml(path)) if path.exists() else []
    app.output.emit(
        {"valid": not errors, "errors": errors},
        lambda: app.output.outcomes([(False, e) for e in errors] or [(True, "policies.yaml is valid")]),
    )
    return 3 if errors else 0
