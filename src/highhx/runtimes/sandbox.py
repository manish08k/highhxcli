"""Sandboxes: a temporary copy of the project where an agent can work without touching the real one.

    highhx sandbox create            copy the project (secrets left out) into a fresh workspace
    highhx sandbox exec <id> -- …    run a command inside it
    highhx sandbox patch <id>        the changes made inside, as a unified diff
    highhx sandbox apply <id>        apply that diff to the project (an approved file change)
    highhx sandbox destroy <id>      kill everything still running in it and delete it

Isolation, by backend (the strongest available is chosen unless one is asked for):

=============  ==========================================================================
seatbelt       macOS ``sandbox-exec``: writes only inside the workspace, the user's credential
               folders unreadable, network denied (``--network deny``, the default)
bubblewrap     Linux ``bwrap``: read-only system, the workspace as the only writable
               directory, a private /tmp and home, new PID namespace, network namespace on deny
docker         a container with the workspace mounted, ``--network none`` on deny, memory and
               process limits
workspace      only a copy and a scrubbed environment, with NO filesystem or network
               confinement. Used only when asked for explicitly (``--isolation workspace``)
=============  ==========================================================================

Every backend also gets: a scrubbed environment (no host secrets, HOME and TMPDIR inside the
workspace), resource limits (CPU time, file size, memory where enforceable), a per-command
timeout, its own process group (killed on timeout, cancellation and destroy), and an audit trail
(every exec is a ``sandbox.exec`` action and event).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from highhx.computer.providers import Capability
from highhx.core.errors import NotFoundError, UsageError
from highhx.core.events import new_id
from highhx.drivers.base import CapabilityError
from highhx.runtimes.base import NETWORK_POLICIES, ExecResult, ResourceLimits, RuntimeInfo
from highhx.runtimes.process import run_process

if TYPE_CHECKING:
    from highhx.drivers.base import ComputerDriver
    from highhx.execution.cancellation import CancellationToken

BACKENDS = ("seatbelt", "bubblewrap", "docker", "workspace")
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials*",
    "*.keystore",
    "secrets.*",
)
SKIP_DIRS = ("node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".tox")
HOME_SECRETS = (".ssh", ".aws", ".gnupg", ".config/gcloud", ".azure", ".kube", ".docker", ".netrc", ".npmrc", ".pypirc", "Library/Keychains", ".git-credentials")
DOCKER_IMAGE = "python:3.12-slim"


def sandboxes_dir() -> Path:
    from highhx.utils.paths import user_data_dir

    return user_data_dir() / "sandboxes"


# ------------------------------------------------------------------- backends
def available_backends() -> dict[str, Capability]:
    out = {}
    exe = shutil.which("sandbox-exec")
    out["seatbelt"] = Capability("seatbelt", sys.platform == "darwin" and bool(exe), exe or "macOS only (sandbox-exec)")
    bwrap = shutil.which("bwrap")
    out["bubblewrap"] = Capability("bubblewrap", sys.platform.startswith("linux") and bool(bwrap), bwrap or "Linux only: install bubblewrap (bwrap)")
    docker = shutil.which("docker")
    out["docker"] = Capability("docker", bool(docker), docker or "Docker is not installed")
    out["workspace"] = Capability("workspace", True, "a copy and a scrubbed environment only: no filesystem or network confinement")
    return out


def choose_backend(requested: str | None = None) -> str:
    found = available_backends()
    if requested:
        if requested not in found:
            raise UsageError(f"Unknown isolation {requested!r} (expected: {', '.join(BACKENDS)}).")
        if not found[requested].available:
            raise CapabilityError(f"{requested} is not available here: {found[requested].detail}")
        return requested
    for name in ("seatbelt", "bubblewrap", "docker"):
        if found[name].available:
            return name
    raise CapabilityError(
        "No sandbox isolation is available on this computer.",
        hint="Install bubblewrap (Linux) or Docker, or pass --isolation workspace for a copy without confinement.",
    )


def seatbelt_profile(workspace: Path, *, network: str, deny_read: list[Path]) -> str:
    def q(path: Path) -> str:
        return json.dumps(str(path))

    readable_denied = " ".join(f"(subpath {q(p)})" for p in deny_read)
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write* (require-not (require-any "
        f"(subpath {q(workspace)}) (literal \"/dev/null\") (literal \"/dev/tty\") (literal \"/dev/dtracehelper\") (subpath \"/dev/fd\"))))",
    ]
    if readable_denied:
        lines.append(f"(deny file-read* {readable_denied})")
    if network == "deny":
        lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------- sandbox
@dataclass
class SandboxRecord:
    id: str
    backend: str
    network: str
    source: str
    created: float
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    baseline: str = ""
    """The commit the workspace started from (for the patch)."""
    pids: list[int] = field(default_factory=list)
    execs: int = 0
    deny_read: list[str] = field(default_factory=list)
    """More paths commands may not read (besides the user's credential folders)."""

    def to_dict(self) -> dict[str, Any]:
        return {**{k: v for k, v in self.__dict__.items() if k != "limits"}, "limits": self.limits.to_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SandboxRecord:
        limits = ResourceLimits.from_dict(data.get("limits") or {})
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "limits"}
        return cls(**known, limits=limits)


class SandboxRuntime:
    kind = "sandbox"

    def __init__(self, record: SandboxRecord, root: Path) -> None:
        self.record = record
        self.root = root

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def id(self) -> str:
        return self.record.id

    def info(self) -> RuntimeInfo:
        return RuntimeInfo(
            "sandbox",
            self.record.id,
            str(self.workspace),
            self.record.backend,
            self.record.network,
            self.record.limits,
            {"source": self.record.source, "created": self.record.created, "execs": self.record.execs},
        )

    def capabilities(self) -> list[Capability]:
        backend = available_backends()[self.record.backend]
        return [
            Capability("exec", backend.available, backend.detail),
            Capability("desktop", False, "sandboxes have no screen; use a remote computer for computer use"),
        ]

    def driver(self) -> ComputerDriver:
        raise CapabilityError("A sandbox has no screen to operate.", hint="Use the local or an ssh:// computer for desktop tasks.")

    def environment(self) -> dict[str, str]:
        from highhx.execution.isolation import isolated_environment

        home = self.workspace / ".highhx-home"
        tmp = self.workspace / ".highhx-tmp"
        home.mkdir(exist_ok=True)
        tmp.mkdir(exist_ok=True)
        env = isolated_environment(extra={"HOME": str(home), "TMPDIR": str(tmp), "HIGHHX_SANDBOX": self.record.id})
        return {k: v for k, v in env.items() if k not in ("SSH_AUTH_SOCK",)}

    def _argv(self, argv: list[str]) -> list[str]:
        backend = self.record.backend
        ws = self.workspace.resolve()
        if backend == "seatbelt":
            home = Path.home()
            deny = [home / p for p in HOME_SECRETS if (home / p).exists()]
            from highhx.utils.paths import user_config_dir

            deny.append(user_config_dir())  # HighhX credentials
            deny += [Path(p) for p in self.record.deny_read]
            profile = self.root / "profile.sb"
            profile.write_text(seatbelt_profile(ws, network=self.record.network, deny_read=[p.resolve() for p in deny if p.exists()]))
            return ["sandbox-exec", "-f", str(profile), *argv]
        if backend == "bubblewrap":
            base = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", str(Path.home())]
            base += ["--bind", str(ws), str(ws), "--chdir", str(ws), "--unshare-pid", "--die-with-parent", "--new-session"]
            for hidden in self.record.deny_read:
                if Path(hidden).is_dir():
                    base += ["--tmpfs", hidden]
            if self.record.network == "deny":
                base.append("--unshare-net")
            return [*base, *argv]
        if backend == "docker":
            limits = self.record.limits
            base = ["docker", "run", "--rm", "-i", "-v", f"{ws}:/workspace", "-w", "/workspace", "--label", f"highhx.sandbox={self.record.id}"]
            if self.record.network == "deny":
                base += ["--network", "none"]
            if limits.memory_mb:
                base += ["--memory", f"{limits.memory_mb}m"]
            if limits.processes:
                base += ["--pids-limit", str(limits.processes)]
            return [*base, DOCKER_IMAGE, *argv]
        return list(argv)

    def exec(self, argv: list[str], *, timeout: float | None = None, cancel: CancellationToken | None = None) -> ExecResult:
        if not argv:
            raise UsageError("Nothing to run.")
        if not self.workspace.is_dir():
            raise NotFoundError(f"Sandbox {self.id} has no workspace (it was destroyed).")

        def started(pid: int) -> None:
            self.record.pids = [*self.record.pids[-20:], pid]
            self._save()

        result = run_process(
            self._argv(argv),
            cwd=self.workspace,
            timeout=min(timeout or self.record.limits.timeout, self.record.limits.timeout),
            env=self.environment() if self.record.backend != "docker" else None,
            limits=self.record.limits,
            cancel=cancel,
            on_start=started,
        )
        self.record.execs += 1
        self._save()
        return result

    def patch(self) -> str:
        """Everything changed in the workspace since it was created, as a unified diff."""
        git = ["git", "-C", str(self.workspace)]
        env = {**os.environ, "GIT_AUTHOR_NAME": "HighhX Sandbox", "GIT_AUTHOR_EMAIL": "sandbox@highhx.invalid", "GIT_COMMITTER_NAME": "HighhX Sandbox", "GIT_COMMITTER_EMAIL": "sandbox@highhx.invalid"}
        subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=env)  # .highhx-home/-tmp are git-excluded
        out = subprocess.run([*git, "diff", "--cached", "--binary", self.record.baseline], check=True, capture_output=True, env=env)
        subprocess.run([*git, "reset", "-q"], check=False, capture_output=True, env=env)
        return out.stdout.decode("utf-8", "replace")

    def stop(self) -> None:
        """Kill every process the sandbox started (their process groups)."""
        import signal

        for pid in self.record.pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if self.record.backend == "docker" and shutil.which("docker"):
            ids = subprocess.run(["docker", "ps", "-q", "--filter", f"label=highhx.sandbox={self.record.id}"], capture_output=True, text=True, check=False).stdout.split()
            if ids:
                subprocess.run(["docker", "kill", *ids], capture_output=True, check=False)
        self.record.pids = []

    def _save(self) -> None:
        (self.root / "sandbox.json").write_text(json.dumps(self.record.to_dict(), indent=2))


class SandboxManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or sandboxes_dir()

    def create(
        self,
        source: Path,
        *,
        isolation: str | None = None,
        network: str = "deny",
        limits: ResourceLimits | None = None,
        copy: bool = True,
        deny_read: list[Path] | None = None,
    ) -> SandboxRuntime:
        if network not in NETWORK_POLICIES:
            raise UsageError(f"Network policy must be one of: {', '.join(NETWORK_POLICIES)}.")
        backend = choose_backend(isolation)
        sandbox_id = new_id("sbx")
        root = self.root / sandbox_id
        workspace = root / "workspace"
        root.mkdir(parents=True)
        if copy:
            shutil.copytree(source, workspace, ignore=shutil.ignore_patterns(*SECRET_PATTERNS, *SKIP_DIRS), symlinks=True)
        else:
            workspace.mkdir()
        baseline = _baseline(workspace)
        record = SandboxRecord(
            sandbox_id, backend, network, str(source), time.time(), limits or ResourceLimits(), baseline,
            deny_read=[str(p.resolve()) for p in deny_read or []],
        )
        runtime = SandboxRuntime(record, root)
        runtime._save()
        return runtime

    def get(self, sandbox_id: str) -> SandboxRuntime:
        if not sandbox_id.replace("_", "").isalnum():
            raise UsageError(f"Invalid sandbox id {sandbox_id!r}.")
        root = self.root / sandbox_id
        meta = root / "sandbox.json"
        if not meta.is_file():
            raise NotFoundError(f"No sandbox {sandbox_id!r}.", hint="See `highhx sandbox list`.")
        return SandboxRuntime(SandboxRecord.from_dict(json.loads(meta.read_text())), root)

    def all(self) -> list[SandboxRuntime]:
        if not self.root.is_dir():
            return []
        out = []
        for meta in sorted(self.root.glob("*/sandbox.json")):
            try:
                out.append(SandboxRuntime(SandboxRecord.from_dict(json.loads(meta.read_text())), meta.parent))
            except (ValueError, TypeError):
                continue
        return out

    def destroy(self, sandbox_id: str) -> None:
        runtime = self.get(sandbox_id)
        runtime.stop()
        shutil.rmtree(runtime.root, ignore_errors=True)


def _baseline(workspace: Path) -> str:
    """Commit the starting state inside the workspace (its own repository or a fresh one) so
    the patch is exactly what changed in the sandbox."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "HighhX Sandbox", "GIT_AUTHOR_EMAIL": "sandbox@highhx.invalid", "GIT_COMMITTER_NAME": "HighhX Sandbox", "GIT_COMMITTER_EMAIL": "sandbox@highhx.invalid"}
    git = ["git", "-C", str(workspace)]
    if not (workspace / ".git").exists():
        subprocess.run([*git, "init", "-q"], check=True, capture_output=True, env=env)
    (workspace / ".git" / "info").mkdir(parents=True, exist_ok=True)
    with (workspace / ".git" / "info" / "exclude").open("a") as handle:
        handle.write("\n.highhx-home/\n.highhx-tmp/\n")
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run([*git, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "--no-verify", "-m", "highhx sandbox baseline"], check=True, capture_output=True, env=env)
    out = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env)
    return out.stdout.strip()
