"""Security, diagnostics, dependencies, deployment, workflows and history — HighhX's DevOps
services exposed to the agent."""

from __future__ import annotations

from typing import Any

from highhx.agent.tools.base import Tool, ToolContext, ToolError, ToolResult, truncate
from highhx.approvals.risk import RiskLevel
from highhx.cloud.plans import AGENT_DEPLOY
from highhx.core.errors import HighhXError
from highhx.safety.actions import ActionKind
from highhx.utils.validation import Any_, Bool, Int, List, Map, Obj, Prop, Str

SECURITY_CHECKS = ("secrets", "permissions", "config", "policy", "workflows", "dependencies")


class SecurityScanTool(Tool):
    name = "security_scan"
    label = "Scanning for security issues"
    description = """
Run HighhX's local security checks: committed secrets, file permissions, insecure
configuration, policy violations, risky workflows and vulnerable dependencies. Findings
include location and remediation; secret values are never included.
"""
    schema = Obj({"checks": Prop(List(Str(choices=SECURITY_CHECKS)), description="Subset of checks (default all).")})

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        from highhx.commands.security.main import run_scan

        report = run_scan(ctx.app, args.get("checks") or SECURITY_CHECKS)
        counts = {k: v for k, v in report.counts().items() if v}
        summary = ", ".join(f"{n} {sev}" for sev, n in counts.items()) or "no findings"
        return ToolResult.json(report.to_dict(), summary=f"Security: {summary}")


class DoctorTool(Tool):
    name = "doctor"
    label = "Checking environment"
    description = "Check required tools and versions, configuration, workflows, environment variables and ports."

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        from highhx.diagnostics.doctor import run_doctor, suggested_actions

        checks = run_doctor(ctx.app)
        failed = [c for c in checks if str(c.status) == "fail"]
        warned = [c for c in checks if str(c.status) == "warn"]
        data = {"checks": [c.to_dict() for c in checks], "suggested_actions": suggested_actions(checks)}
        return ToolResult.json(data, summary=f"{len(failed)} problem(s), {len(warned)} warning(s)")


class DiagnoseTool(Tool):
    name = "diagnose"
    label = "Diagnosing HighhX setup"
    description = "Identify concrete problems in the HighhX project setup and whether `repair` can fix them."

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        from highhx.diagnostics.diagnose import diagnose

        found = diagnose(ctx.app)
        return ToolResult.json([d.to_dict() for d in found], summary=f"{len(found)} issue(s)")


class RepairTool(Tool):
    name = "repair"
    label = "Repairing HighhX setup"
    mutating = True
    risk = RiskLevel.NORMAL
    description = (
        "Apply HighhX's automatic repairs for problems reported by `diagnose` (missing dirs, default workflows …)."
    )

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        from highhx.diagnostics.diagnose import diagnose
        from highhx.diagnostics.repair import run_repairs

        found = [d for d in diagnose(ctx.app) if d.repair]
        if not found:
            return ToolResult("Nothing to repair.", summary="nothing to repair")
        perms = ctx.permissions
        action = perms.action(
            ActionKind.WRITE_FILE,
            f"Repair {len(found)} HighhX setup issue(s)",
            tool=self.name,
            target=".highhx/",
            repairs=",".join(sorted({str(d.repair) for d in found})),
        )
        authorization = perms.authorize(action, policy_action="agent:repair", details=[d.problem for d in found])
        with perms.executing(authorization) as event:
            results = run_repairs(ctx.app, found)
            event.verified = all(r.applied for r in results)
        return ToolResult.json([r.to_dict() for r in results], summary=f"{sum(r.applied for r in results)} repaired")


class DependenciesTool(Tool):
    name = "dependencies"
    label = "Checking dependencies"
    description = """
Report dependency managers and manifests; optionally list outdated packages and run a
vulnerability audit (these may call the package manager and the network).
"""
    schema = Obj({"outdated": Prop(Bool()), "audit": Prop(Bool())})

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        deps = ctx.app.dependencies
        data: dict[str, Any] = {"managers": deps.summary()}
        notes = []
        if args.get("outdated"):
            reports = deps.outdated()
            data["outdated"] = [r.to_dict() for r in reports]
            notes.append(f"{sum(len(r.packages) for r in reports)} outdated")
        if args.get("audit"):
            audits = deps.audit()
            data["audit"] = [a.to_dict() for a in audits]
            notes.append(f"{sum(len(a.vulnerabilities) for a in audits)} vulnerable")
        return ToolResult.json(data, summary=", ".join(notes) or f"{len(data['managers'])} manager(s)")


class DeployTargetsTool(Tool):
    name = "deploy_targets"
    label = "Checking deployments"
    description = "Configured deploy targets (type, production flag) with their latest deployment and live status."

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        if not ctx.app.config.deploy_targets:
            return ToolResult(
                "No deploy targets are configured (deploy.targets in .highhx/config.yaml).", summary="no targets"
            )
        rows = ctx.app.deployments.status(live=True)
        return ToolResult.json(rows, summary=f"{len(rows)} target(s)")


class DeployTool(Tool):
    name = "deploy"
    label = "Deploying"
    mutating = True
    feature = AGENT_DEPLOY
    risk = RiskLevel.DANGEROUS
    description = """
Deploy to a configured target with HighhX's deployment pipeline: preflight checks →
approval → deploy → health check (→ automatic rollback if configured). Deployments always
require the user's explicit approval; production targets require typed confirmation.
Only deploy when the user asked for it. Verify tests pass first.
"""
    schema = Obj({"target": Prop(Str(min_length=1), required=True), "version": Prop(Str())})

    def describe(self, args: dict[str, Any]) -> str:
        return f"Deploy to {args.get('target')}"

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        manager = ctx.app.deployments
        target = manager.target(str(args["target"]))
        perms = ctx.permissions
        action = perms.action(
            ActionKind.DEPLOY,
            f"Deploy application to {'PRODUCTION (' + target.name + ')' if target.production else target.name}",
            tool=self.name,
            target=target.name,
            application=f"{target.type} deployment",
            environment="production" if target.production else None,
            production=target.production,
            version=args.get("version") or "current",
        )
        # Always an explicit question — even when the strategy's risk would be auto-approved.
        authorization = perms.authorize(
            action,
            policy_action=f"agent:deploy:{target.name}",
            always_confirm=True,
            details=[f"target: {target.name} ({target.type})", f"version: {args.get('version') or 'current'}"],
        )
        with perms.executing(authorization) as event:
            outcome = manager.deploy(target.name, version=args.get("version"))
            event.verified = outcome.ok  # deploy success includes the configured health check
            if not outcome.ok:
                event.status = "failed"
        status = "deployed" if outcome.ok else ("rolled back" if outcome.rolled_back else "failed")
        return ToolResult.json(outcome.to_dict(), ok=outcome.ok, summary=f"{target.name}: {status}")


class RollbackTool(Tool):
    name = "rollback"
    label = "Rolling back"
    mutating = True
    feature = AGENT_DEPLOY
    risk = RiskLevel.DANGEROUS
    description = "Roll a deploy target back to its previous successful deployment. Always requires approval."
    schema = Obj({"target": Prop(Str(min_length=1), required=True)})

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        manager = ctx.app.deployments
        target = manager.target(str(args["target"]))
        perms = ctx.permissions
        action = perms.action(
            ActionKind.ROLLBACK,
            f"Roll back {target.name}",
            tool=self.name,
            target=target.name,
            application=f"{target.type} deployment",
            environment="production" if target.production else None,
            production=target.production,
        )
        authorization = perms.authorize(
            action, policy_action=f"agent:rollback:{target.name}", always_confirm=True, details=[f"type: {target.type}"]
        )
        with perms.executing(authorization) as event:
            outcome = manager.rollback(target.name)
            event.verified = outcome.ok
        return ToolResult.json(
            outcome.to_dict(), ok=outcome.ok, summary=f"{target.name}: {'rolled back' if outcome.ok else 'failed'}"
        )


class WorkflowsTool(Tool):
    name = "list_workflows"
    label = "Listing workflows"
    description = "HighhX workflows defined for this project (.highhx/workflows) with their steps."

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        if not ctx.app.initialized:
            return ToolResult("This project is not initialized with HighhX, so it has no workflows.")
        rows = []
        for ref in ctx.app.workflow_loader.list():
            try:
                spec = ctx.app.workflow_loader.load(ref.key)
                rows.append(
                    {
                        "key": ref.key,
                        "name": spec.name,
                        "description": spec.description,
                        "steps": [step.id for step in spec.steps],
                    }
                )
            except HighhXError as exc:
                rows.append({"key": ref.key, "error": exc.message})
        return ToolResult.json(rows, summary=f"{len(rows)} workflow(s)")


class RunWorkflowTool(Tool):
    name = "run_workflow"
    label = "Running workflow"
    mutating = True
    risk = RiskLevel.NORMAL
    requires_project = True
    description = """
Run a HighhX workflow (e.g. ci, test, build, release) with the workflow engine: dependency
ordering, parallel steps, retries, timeouts and per-step approvals all apply.
"""
    schema = Obj({"name": Prop(Str(min_length=1), required=True), "inputs": Prop(Map(Any_()))})

    def describe(self, args: dict[str, Any]) -> str:
        return f"Run workflow {args.get('name')}"

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        from highhx.workflows.engine import NullReporter, WorkflowEngine

        app = ctx.app
        name = str(args["name"])
        if name not in app.workflow_loader:
            raise ToolError(f"No workflow named '{name}'. Available: {', '.join(app.workflow_loader.keys()) or 'none'}")
        perms = ctx.permissions
        action = perms.action(
            ActionKind.EXEC, f"Run workflow {name}", tool=self.name, target=name, command=f"highhx run {name}"
        )
        authorization = perms.authorize(
            action, policy_action="agent:workflow", grant=f"workflow:{name}", engine_prompts=True
        )
        engine = WorkflowEngine(
            app.engine,
            app.workflow_loader,
            root=app.root,
            reporter=NullReporter(),
            project_name=app.config.project_name,
        )
        with perms.executing(authorization) as event:
            result = engine.run(name, inputs=args.get("inputs") or {}, cancel=ctx.cancel)
            event.verified = result.ok
            if not result.ok:
                event.status = "failed"
        data = result.to_dict()
        return ToolResult(
            truncate(str(data)), ok=result.ok, summary=f"workflow {name}: {'passed' if result.ok else 'failed'}"
        )


class HistoryTool(Tool):
    name = "recent_runs"
    label = "Reading history"
    description = """
Recent HighhX executions (tests, builds, workflows, deploys …) with status and duration, or
the redacted log of one execution when `execution_id` is given. Useful for 'why did it fail?'.
"""
    schema = Obj(
        {
            "limit": Prop(Int(minimum=1, maximum=100), description="Default 15."),
            "execution_id": Prop(Str()),
            "tail": Prop(Int(minimum=1, maximum=2000), description="Log lines (default 300)."),
        }
    )

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        app = ctx.app
        if args.get("execution_id"):
            execution_id = str(args["execution_id"])
            if not app.logs.exists(execution_id):
                raise ToolError(f"No log for execution {execution_id}")
            lines = app.logs.read(execution_id, tail=int(args.get("tail") or 300))
            return ToolResult(truncate("\n".join(lines)), summary=f"{len(lines)} log line(s)")
        if app.history is None:
            return ToolResult("History storage is unavailable.")
        records = app.history.list(limit=int(args.get("limit") or 15))
        rows = [
            {
                "id": r.id,
                "kind": r.kind,
                "name": r.name,
                "status": r.status,
                "exit_code": r.exit_code,
                "started_at": r.started_at,
                "duration": r.duration,
                "error": r.error,
            }
            for r in records
        ]
        return ToolResult.json(rows, summary=f"{len(rows)} run(s)")


class RememberTool(Tool):
    name = "remember"
    label = "Saving to project memory"
    untrusted_output = False
    description = """
Save a durable fact about this project for future sessions (conventions, how to run
things, decisions, pitfalls). Keep it to one concise sentence. Never store secrets.
"""
    schema = Obj({"fact": Prop(Str(min_length=3), required=True)})

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(ctx.remember(str(args["fact"])), summary="remembered")
