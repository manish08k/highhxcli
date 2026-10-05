"""``vm.*`` catalog entries: virtual machines (Lima, Tart). Creating, starting and stopping are
medium risk; restoring a snapshot (it discards the current state) and destroying are high;
``vm.exec`` is classified by the command it runs, like any command."""

from __future__ import annotations

from highhx.actions.handlers import vm
from highhx.actions.policy import Risk
from highhx.actions.spec import RUN_PROCESSES, ActionSpec, Handler
from highhx.safety.actions import ActionKind
from highhx.utils.validation import Int, List, Num, Obj, Prop, Str

NAME = Prop(Str(min_length=1), required=True)
BACKEND = Prop(Str(choices=("lima", "tart")), description="Default: the first installed.")


def vm_specs() -> list[ActionSpec]:
    def life(
        name: str,
        description: str,
        handler: Handler,
        risk: Risk,
        kind: ActionKind,
        extra: dict[str, Prop] | None = None,
    ) -> ActionSpec:
        policy = name.replace(".", ":")

        def policy_name(_inputs: object) -> str:
            return policy

        return ActionSpec(
            name,
            description,
            handler,
            Obj({"name": NAME, "backend": BACKEND, **(extra or {})}),
            {"name": "VM", "state": "after"},
            risk,
            kind,
            (RUN_PROCESSES,),
            timeout=3600,
            agent=False,
            target=lambda i: f"vm {i.get('name', '')}",
            policy_action=policy_name,
        )

    return [
        ActionSpec(
            "vm.list",
            "Virtual machines (Lima or Tart) and their state.",
            vm.vm_list,
            Obj({"backend": BACKEND}),
            {"vms": "[{name, status, backend}]"},
            permissions=(RUN_PROCESSES,),
            idempotent=True,
            agent=False,
        ),
        ActionSpec(
            "vm.create",
            "Create a virtual machine (its own disk, network and CPU/memory limits).",
            vm.vm_create,
            Obj(
                {
                    "name": NAME,
                    "backend": BACKEND,
                    "image": Prop(Str(min_length=1)),
                    "cpus": Prop(Int(minimum=1, maximum=64)),
                    "memory_mb": Prop(Int(minimum=512, maximum=262144)),
                    "disk_gb": Prop(Int(minimum=5, maximum=4096)),
                }
            ),
            {"name": "VM"},
            Risk.MEDIUM,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            timeout=3600,
            agent=False,
            target=lambda i: f"vm {i.get('name', '')}",
            policy_action=lambda i: "vm:create",
        ),
        life("vm.start", "Start a VM.", vm.vm_start, Risk.MEDIUM, ActionKind.APP_LAUNCH),
        life("vm.stop", "Stop a VM (its disk is kept).", vm.vm_stop, Risk.MEDIUM, ActionKind.EXEC),
        life("vm.pause", "Suspend a VM (Tart; Lima cannot).", vm.vm_pause, Risk.LOW, ActionKind.EXEC),
        life("vm.resume", "Resume a suspended VM.", vm.vm_resume, Risk.MEDIUM, ActionKind.APP_LAUNCH),
        life(
            "vm.snapshot",
            "Snapshot a VM under a tag.",
            vm.vm_snapshot,
            Risk.LOW,
            ActionKind.WRITE_FILE,
            {"tag": Prop(Str(min_length=1), required=True)},
        ),
        life(
            "vm.restore",
            "Restore a VM from a snapshot (its current state is lost: always asked).",
            vm.vm_restore,
            Risk.HIGH,
            ActionKind.DELETE_FILE,
            {"tag": Prop(Str(min_length=1), required=True)},
        ),
        life(
            "vm.destroy", "Delete a VM and its disk (always asked).", vm.vm_destroy, Risk.HIGH, ActionKind.DELETE_FILE
        ),
        ActionSpec(
            "remote.check",
            "Heartbeat a remote computer (ssh://): reachable, latency, HighhX version there.",
            vm.remote_check,
            Obj({"target": Prop(Str(min_length=7), required=True)}),
            {"reachable": "answers over ssh", "latency_ms": "round trip", "highhx": "remote version"},
            Risk.LOW,
            ActionKind.READ,
            (RUN_PROCESSES,),
            timeout=60,
            agent=False,
            target=lambda i: str(i.get("target", "")),
            policy_action=lambda i: "remote:check",
        ),
        ActionSpec(
            "vm.exec",
            "Run a command inside a running VM (classified by what it does, like any command).",
            vm.vm_exec,
            Obj(
                {
                    "name": NAME,
                    "backend": BACKEND,
                    "command": Prop(Str(min_length=1)),
                    "argv": Prop(List(Str())),
                    "timeout": Prop(Num(minimum=1)),
                }
            ),
            {"exit_code": "exit code", "stdout": "output", "stderr": "errors"},
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            timeout=3600,
            agent=False,
            command=vm.command_of,
            target=lambda i: f"vm {i.get('name', '')}",
            policy_action=lambda i: "vm:exec",
        ),
    ]
