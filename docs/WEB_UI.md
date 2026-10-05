# Web console

```text
highhx web            # prints http://127.0.0.1:PORT/?token=…   (Ctrl-C stops it)
```

A browser view of the same runtime as the CLI and TUI: start a task (steps, or HighhX Free's resolver),
pause/resume/cancel it, answer approvals (approve, reject, modify, defer; critical actions need the typed
word), watch the plan, actions, tool calls, network evidence and events, browse task history and each
task's steps, artifacts and benchmark results, and see a live view of the browser (Chrome's screencast)
or the desktop (screenshots, ≤ 2 fps).

Security: bound to 127.0.0.1; a random per-run token on every request (constant-time comparison); the
Host header must be loopback (DNS rebinding); state changes need the token in `X-HighhX-Token` and a
same-origin `Origin` (CSRF); a strict Content-Security-Policy with a per-response script nonce; every
value from the runtime is shown as text, never HTML (tested in real Chrome with an injection payload).
Tasks run one at a time (the executor's cancellation is per application). Frames are never stored.

API (for scripts, same token): `GET /api/state`, `/api/events?after=N`, `/api/history[/ID]`,
`/api/timeline/TRACE`, `/api/approvals`, `/api/artifacts`, `/api/benchmarks`, `/api/frame?source=browser`;
`POST /api/tasks`, `/api/tasks/ID/{pause,resume,cancel}`, `/api/approvals/ID`.
