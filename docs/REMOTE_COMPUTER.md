# Remote computers and virtual machines

## Remote computers (SSH)

`HIGHHX_COMPUTER_TARGET=ssh://user@host[:port]` makes the desktop driver and runtime operate another
computer through HighhX running there. SSH runs with `BatchMode=yes` (keys only, never passwords) and,
when a `known_hosts` file is given, strict host-key checking. Nothing is ever exposed through an
unauthenticated tunnel. The `remote.check` action is the heartbeat: reachable, round-trip
latency, and the HighhX version there (its desktop needs HighhX). Status: implemented, tested with a
stand-in `ssh`; not validated against a real second machine in this build (experimental).

## Virtual machines (Lima, Tart)

`highhx vm` (actions `vm.*`): `create`, `start`, `pause`, `resume`, `stop`, `destroy`, `snapshot`,
`restore`, `exec`, `list` — through **Lima** (Linux VMs on macOS/Linux) or **Tart** (macOS/Linux VMs on
Apple silicon), driven through their command lines. A VM has its own filesystem, kernel and network
and the CPU/memory/disk limits given at creation. Lima cannot pause (refused, not faked). `vm.exec` is
classified by the command it runs (`rm -rf /` is critical inside a VM too); restoring a snapshot and
destroying are high risk and always asked. A VM's desktop is operated by installing HighhX inside it
and connecting as an `ssh://` computer.

Status: implemented and tested against fake `limactl`/`tart` command lines; neither tool is installed
on the build machine, so real VMs are **pending validation**. Without them, `highhx vm` says what to
install.
