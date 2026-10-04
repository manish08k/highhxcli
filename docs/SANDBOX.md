# Sandboxes

A sandbox is a temporary copy of the project where commands (and agents) work without touching
the real project or the rest of the computer.

```text
highhx sandbox create [--isolation seatbelt|bubblewrap|docker|workspace] [--network deny|allow] [--timeout S] [--memory MB] [--empty]
highhx sandbox exec SBX -- pytest -q
highhx sandbox patch SBX            # the changes, as a unified diff
highhx sandbox apply SBX            # apply them to the project (shown and asked first)
highhx sandbox list · destroy SBX · destroy --all (the kill switch)
```

Code: [`src/highhx/runtimes/`](../src/highhx/runtimes) · actions `sandbox.*`.

## Isolation backends

The strongest available backend is chosen unless one is asked for.

| Backend | Platform | Filesystem | Network (`deny`, the default) | Process |
|---|---|---|---|---|
| `seatbelt` | macOS `sandbox-exec` | writes only inside the workspace; the user's credential folders (`.ssh`, `.aws`, `.gnupg`, keychains, …), HighhX's config directory and any `deny_read` paths are unreadable | all network denied | own process group, rlimits |
| `bubblewrap` | Linux `bwrap` | read-only system, the workspace as the only writable directory, private `/tmp` and home | new network namespace | new PID namespace, dies with its parent |
| `docker` | Docker | only the workspace mounted | `--network none` | memory and process limits |
| `workspace` | any | **a copy only, no confinement** | **not restricted** | process group, rlimits |

`workspace` is never chosen automatically, and creating one (or any sandbox with `--network
allow`) is a medium-risk action that is asked first.

Every backend also gets:
- a scrubbed environment (only safe variables, `HOME` and `TMPDIR` inside the workspace, no `SSH_AUTH_SOCK`);
- resource limits (CPU seconds and file size; memory on Linux and Docker);
- a per-command timeout;
- process-group cleanup on timeout, cancellation and destroy (background children never outlive the command);
- an audit trail: `sandbox.exec` actions, plus `sandbox.*` and `runtime.*` events.

The copy leaves out secrets (`.env*`, keys, certificates, `credentials*`, `.npmrc`, `.pypirc`, …)
and heavy directories (`node_modules`, virtualenvs, caches). The starting state is committed
inside the workspace, so `patch` is exactly what changed there. **A command run in a sandbox is
classified like any command:** a sandbox limits what it can reach and never lowers its risk
(`rm -rf /` is still critical). Policy names: `sandbox:create`, `sandbox:exec`,
`sandbox:apply`, `sandbox:destroy`.

**What has run where:** Seatbelt enforcement (writes outside the workspace, reads of denied
folders and network access all refused) is tested against the real macOS sandbox. Bubblewrap
and Docker are implemented, and their tests run only where they are installed; they have not
been exercised in this build's environment.

## Runtimes

`Runtime` is the common interface (`info`, `capabilities`, `exec`, `driver`, `stop`):
`LocalRuntime` (this computer), `SandboxRuntime`, `RemoteRuntime` (another computer over SSH:
keys only, host keys checked). `VMRuntime` and `CloudRuntime` are **not implemented** and raise
a capability error naming the alternatives. See [COMPUTER_RUNTIME.md](COMPUTER_RUNTIME.md).
