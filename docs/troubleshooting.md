# Troubleshooting

Start with:

```bash
highhx doctor        # tools, versions, config, env vars, ports
highhx diagnose      # concrete problems, and which ones `repair` can fix
highhx repair        # safe, deterministic repairs (asks before anything risky)
highhx debug         # redacted bundle to paste into a bug report
```

## Common problems

**"Not approved: … no interactive terminal is available"** (exit 6)
HighhX will not guess in CI or pipes. Re-run in a terminal, or pass `--yes` if the
action is within `approvals.yes_max_risk`. Non-bypassable actions always need a terminal.

**"Blocked by project policy"** (exit 7)
A rule in `.highhx/policies.yaml` denies the action. `highhx policy` lists the rules.

**"This directory is not a HighhX project"** (exit 3)
Run `highhx init` in the project root, or use `-C path/to/project`.

**"Invalid configuration"** (exit 3)
`highhx config validate` lists every problem with its path, e.g.
`services.api.port: must be <= 65535`.

**A workflow step "was not found on PATH"**
The tool is missing or only available in a virtualenv. Use `uv run …`, `poetry run …`,
an absolute path, or activate the environment before running HighhX.

**"Port 8000 needed by 'api' is already in use by python 1234"**
Another process holds the port. `highhx ports` shows owners; stop it or change
`services.api.port`.

**"Docker is not running."**
Start Docker Desktop / the docker service. `highhx doctor` checks the daemon.

**Deployment "Preflight checks failed … working tree: uncommitted changes"**
`policies.require_clean_tree` includes `deploy`. Commit or stash first.

**Code plugin shows "blocked … these exact files were not trusted by you"**
The plugin came with the repository, or its files changed after you installed it.
Review the code, then run `highhx plugin trust <name>` (or reinstall with
`highhx plugin install <source> --force`).

**"history storage unavailable (file is not a database)"**
The state database is damaged. HighhX keeps working without history;
`highhx repair` moves the damaged file aside (it is kept as `highhx.db.corrupt-<time>`)
and creates a new one.

**Stale "running" executions after a crash**
They are marked cancelled automatically on the next run; `highhx repair` also does it.

## Getting more detail

- `--verbose` prints each command before it runs.
- `--debug` shows Python tracebacks for unexpected errors.
- `highhx logs <id>` and `highhx trace <id>` show what happened in a past run.
- `HIGHHX_ASCII=1` switches to ASCII symbols for terminals without UTF-8.
