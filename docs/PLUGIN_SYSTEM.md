# Plugin system

User guide (manifest reference, installing, trusting): [plugins.md](plugins.md). This
document describes the architecture and the boundaries.

## Kinds of plugins

| Kind | Contributes | Runs |
|---|---|---|
| Declarative (`highhx-plugin.yaml`) | commands (a command line + declared risk), workflows, templates, detectors | Commands through the engine with an isolated environment |
| Code (`entry: plugin.py:Class`) | click commands, detectors, deployment strategies, cloud providers, workflow and template dirs | Python in the HighhX process — only with `plugins.allow_code: true` **and** a per-user trust record for the plugin's exact contents (SHA-256) |

Plugins declare `permissions` (`commands`, `subprocess`, `network`, `filesystem`, `env`,
`deploy`, `detectors`); the plugin API refuses registrations the manifest did not declare.
Installation locks the plugin's contents; a repository can never trust its own code; symbolic
links and path-traversing names are rejected.

## Plugins and the action engine

Each enabled plugin's declared commands become actions for the project:

```text
plugin.<plugin>.<command>      e.g. plugin.acme.lint-docs
```

| Boundary | Rule |
|---|---|
| Risk | The declared risk is a floor that can only be raised — never below LOW; the classifier still rates the concrete command line |
| Execution | Through the plugin command itself: isolated environment (no project secrets), policy action `plugin:<plugin>:<command>` |
| AI agent | Never offered to the agent's `run_actions`; plugins cannot extend what the AI can do |
| Failure | A broken or disabled plugin never breaks the built-in catalog |

They appear in `/tools`, `/run`, `highhx actions`, and can be used in workflows as
`action: plugin.acme.lint-docs`.

## Extension points in the core

| Extension | API |
|---|---|
| New built-in action | `ActionSpec` in `actions/catalog.py` ([ACTION_ENGINE.md](ACTION_ENGINE.md#adding-an-action)) |
| Plain-language rule | `RULES` in `actions/resolver.py` |
| Deployment target type | `register_strategy(type, factory)` / plugin `add_deployment_strategy` |
| Cloud provider | `integrations.cloud.register_provider` / plugin `add_cloud_provider` |
| Detector | plugin `add_detector` or declarative `detectors` |
| Project template | `templates/<stack>/` or plugin `add_template_dir` |
| Workflow library | plugin `add_workflow_dir` / declarative `workflows` |

Tests: `tests/unit/test_plugins.py` (install, lock, trust, isolation),
`tests/unit/actions/test_agent_and_boundaries.py` (plugin actions).
