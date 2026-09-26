# Contributing to HighhX

Thanks for helping! This guide covers the practical bits.

## Getting set up

```bash
# from a checkout of this repository
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest && ruff check . && mypy
```

## Ground rules

- **No placeholders.** Every feature must work end to end and have tests. No `pass`,
  `TODO` or dummy return values outside abstract interfaces.
- **Local-first.** No mandatory network access, cloud services, accounts, API keys or AI features.
- **Safe by default.** Anything that changes shared state, deletes data or publishes must go
  through `Engine.approve()` / `Engine.run()` with an honest `RiskLevel`, and must honour `--dry-run`.
- **Never print secrets.** Use `mask()` for display and make sure logs go through the redactor.
- **Cross-platform.** Don't assume bash, `/usr/bin` or GNU tools; prefer Python APIs and
  `highhx.utils.platform` for platform differences.
- **Thin commands.** CLI modules in `highhx/commands/` parse arguments and render results;
  logic belongs in the domain packages (see [docs/architecture.md](docs/architecture.md)).

## Tests

- Unit tests live in `tests/unit/<area>/`, integration tests in `tests/integration/`,
  end-to-end tests (real subprocess) in `tests/e2e/`, security guarantees in `tests/security/`.
- Use the fixtures in `tests/conftest.py` (`cli`, `make_engine`, `python_project`,
  `git_python_project`). They isolate user directories and git identity, so tests never
  depend on your machine's configuration.
- External systems (Docker, SSH, Kubernetes, registries) are faked; SQLite, git and
  subprocesses are real.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:` …);
  HighhX's own changelog is generated from them.
- Keep pull requests focused; include tests and update `docs/` when behaviour changes.
  `docs/commands.md` is generated from the CLI — regenerate it with
  `python scripts/generate_docs.py` when you change commands and `schemas/` with
  `python scripts/generate_schemas.py` when you change a schema (the test suite fails
  when either drifts).
