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
`POST /api/tasks` (with a `request_id`, a repeated request returns the task it started),
`/api/tasks/ID/{pause,resume,cancel}`, `/api/approvals/ID`, `/api/computer/{take,release}`.

**Human takeover.** *Take over* hands the computer to you: HighhX's actions that would change it
(browser, desktop, Android) are refused — whoever asks — and a running task waits (looking is still
allowed, so the live view keeps working). Every screenshot taken before stops grounding clicks.
*Release* hands it back: the task observes the computer again and decides from what is there now;
the step it was about to take had not run, so nothing is repeated.
