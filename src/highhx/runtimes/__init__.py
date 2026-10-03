"""Runtimes: local, sandbox, remote (VM and cloud report a capability error). See
docs/COMPUTER_RUNTIME.md and docs/SANDBOX.md."""

from highhx.runtimes.base import CloudRuntime, ExecResult, LocalRuntime, ResourceLimits, Runtime, RuntimeInfo, VMRuntime
from highhx.runtimes.remote import RemoteRuntime
from highhx.runtimes.sandbox import SandboxManager, SandboxRuntime, available_backends, choose_backend

__all__ = [
    "CloudRuntime",
    "ExecResult",
    "LocalRuntime",
    "RemoteRuntime",
    "ResourceLimits",
    "Runtime",
    "RuntimeInfo",
    "SandboxManager",
    "SandboxRuntime",
    "VMRuntime",
    "available_backends",
    "choose_backend",
]
