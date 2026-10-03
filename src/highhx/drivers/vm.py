"""VMDriver: virtual machines are not implemented in this build.

The interface is here so a backend (a local hypervisor, a cloud desktop API, a Cua-style
container computer) can be added behind ComputerDriver without touching the agent loop. Until
then, every use is a clear :class:`~highhx.drivers.base.CapabilityError`. HighhX does not
pretend to have a VM. For isolation today, use ``highhx sandbox`` (process and filesystem
sandboxes) or a remote computer over SSH (``RemoteDriver``).
"""

from __future__ import annotations

from highhx.drivers.base import CapabilityError, DriverCapabilities, unsupported

DETAIL = "no virtual-machine backend is implemented in this build (use `highhx sandbox` or an ssh:// computer)"


class VMDriver:
    surface = "desktop"
    name = "vm"

    def __init__(self, spec: str = "") -> None:
        raise CapabilityError(
            "Virtual machines are not supported yet.",
            hint="Use `highhx sandbox create` for an isolated workspace, or HIGHHX_COMPUTER_TARGET=ssh://… for another computer.",
        )

    @staticmethod
    def capabilities() -> DriverCapabilities:
        return DriverCapabilities("vm", "desktop", unsupported("vm", DETAIL))
