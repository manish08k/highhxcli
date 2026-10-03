"""``sandbox.*`` catalog entries."""

from __future__ import annotations

from highhx.actions.handlers import sandbox
from highhx.actions.policy import Risk
from highhx.actions.spec import RUN_PROCESSES, SANDBOX, WRITE_PROJECT, ActionSpec, Inputs
from highhx.cloud.plans import AGENT_COMMANDS
from highhx.safety.actions import ActionKind
from highhx.utils.validation import Bool, Int, List, Num, Obj, Prop, Str

ID = Prop(Str(min_length=1), required=True, description="The sandbox id (see sandbox.list).")


def _create_risk(inputs: Inputs) -> Risk:
    if inputs.get("network") == "allow" or inputs.get("isolation") == "workspace":
        return Risk.MEDIUM  # weaker isolation: the person decides
    return Risk.LOW


def sandbox_specs() -> list[ActionSpec]:
    return [
        ActionSpec(
            "sandbox.create",
            "Create a sandbox: a copy of the project (secrets left out) with filesystem, network, environment and "
            "resource isolation. Network is denied unless `network: allow`.",
            sandbox.create,
            Obj(
                {
                    "isolation": Prop(Str(choices=("seatbelt", "bubblewrap", "docker", "workspace"))),
                    "network": Prop(Str(choices=("deny", "allow"))),
                    "timeout": Prop(Num(minimum=1)),
                    "cpu_seconds": Prop(Int(minimum=1)),
                    "file_mb": Prop(Int(minimum=1)),
                    "memory_mb": Prop(Int(minimum=0)),
                    "processes": Prop(Int(minimum=0)),
                    "copy": Prop(Bool()),
                }
            ),
            {"id": "the sandbox id", "workspace": "its directory", "isolation": "the backend"},
            Risk.LOW,
            ActionKind.READ,
            (SANDBOX,),
            timeout=600,
            feature=AGENT_COMMANDS,
            risk_for=_create_risk,
            policy_action=lambda i: "sandbox:create",
        ),
        ActionSpec("sandbox.list", "List sandboxes.", sandbox.list_, Obj({}), {"sandboxes": "[…]"}, Risk.SAFE, ActionKind.READ, (SANDBOX,), idempotent=True, feature=AGENT_COMMANDS),
        ActionSpec(
            "sandbox.exec",
            "Run a command inside a sandbox (classified by what it does, like any command).",
            sandbox.exec_,
            Obj({"id": ID, "command": Prop(Str(min_length=1)), "argv": Prop(List(Str())), "timeout": Prop(Num(minimum=1))}),
            {"exit_code": "exit code", "stdout": "output", "stderr": "errors"},
            Risk.LOW,
            ActionKind.EXEC,
            (SANDBOX, RUN_PROCESSES),
            timeout=3600,
            feature=AGENT_COMMANDS,
            command=sandbox.command_of,
            target=lambda i: str(i.get("id", "")),
            policy_action=lambda i: "sandbox:exec",
        ),
        ActionSpec(
            "sandbox.patch",
            "The changes made inside a sandbox, as a unified diff.",
            sandbox.patch,
            Obj({"id": ID}),
            {"patch": "unified diff", "files": "changed files"},
            Risk.SAFE,
            ActionKind.READ,
            (SANDBOX,),
            idempotent=True,
            feature=AGENT_COMMANDS,
        ),
        ActionSpec(
            "sandbox.apply",
            "Apply a sandbox's changes to the project (checked with git apply first).",
            sandbox.apply,
            Obj({"id": ID}),
            {"files": "applied files"},
            Risk.MEDIUM,
            ActionKind.WRITE_FILE,
            (SANDBOX, WRITE_PROJECT),
            timeout=300,
            feature=AGENT_COMMANDS,
            target=lambda i: f"the project (from sandbox {i.get('id', '')})",
            preview=sandbox.apply_preview,
            policy_action=lambda i: "sandbox:apply",
        ),
        ActionSpec(
            "sandbox.destroy",
            "Kill everything running in a sandbox and delete it (all: every sandbox — the kill switch).",
            sandbox.destroy,
            Obj({"id": Prop(Str(min_length=1)), "all": Prop(Bool())}),
            {"destroyed": "ids"},
            Risk.LOW,
            ActionKind.READ,
            (SANDBOX,),
            feature=AGENT_COMMANDS,
            policy_action=lambda i: "sandbox:destroy",
        ),
    ]
