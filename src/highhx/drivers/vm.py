"""VMDriver: a virtual machine's desktop.

VMs themselves are managed by :mod:`highhx.runtimes.vm` (Lima or Tart: create, start, pause,
snapshot, restore, exec). Their desktop is operated by HighhX running *inside* the VM, reached
as a remote computer over SSH — so this driver is the remote driver for that target. Without a
backend, or without HighhX in the VM, every use is a clear capability error.
"""

from __future__ import annotations

from highhx.drivers.base import CapabilityError, DriverCapabilities, unsupported


def _detail() -> str:
    from highhx.runtimes.vm import available_vm_backends

    found = [n for n, c in available_vm_backends().items() if c.available]
    if found:
        return f"VM backends: {', '.join(found)}; a VM's desktop needs HighhX inside it (ssh://)"
    return "no VM backend installed (lima or tart); use `highhx sandbox` or an ssh:// computer"


DETAIL = "VMs through lima or tart (see `highhx vm`); a VM's desktop needs HighhX inside it (ssh://)"


class VMDriver:
    surface = "desktop"
    name = "vm"

    def __init__(self, spec: str = "") -> None:
        raise CapabilityError(
            "A VM's desktop is operated through HighhX running inside the VM.",
            hint="Create and start it with `highhx vm`, install HighhX there, then use HIGHHX_COMPUTER_TARGET=ssh://… .",
        )

    @staticmethod
    def capabilities() -> DriverCapabilities:
        return DriverCapabilities("vm", "desktop", unsupported("vm", _detail()))
