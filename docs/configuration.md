# Configuration

HighhX reads `.highhx/config.yaml` (validated against
[schemas/config.schema.json](../schemas/config.schema.json)). Validate everything with:

```bash
highhx config validate        # config, environment, policies and all workflows
highhx config show            # effective config (with --config-profile overlay)
highhx config get commands.test
highhx config schema workflow # print a JSON Schema for editors
```

Unknown keys and duplicate keys are errors ("`comands`: unknown field (did you mean 'commands'?)"), and
every problem is reported at once.

## Reference

```yaml
version: 1

project:
  name: my-app                 # defaults to the manifest name
  type: python                 # informational
  description: ""

commands:                      # override detected commands
  dev: …        start: …       test: …       coverage: …
  build: …      package: …     lint: …       format: …
  fix: …        typecheck: …   install: …    update: …
  clean: …      check: …

scripts:                       # highhx script <name> [args…]
  seed-demo: python scripts/seed.py

tasks:                         # highhx task <name>; dependencies run first
  generate: {run: make gen}
  ship:
    description: Build and upload
    run: [make build, make upload]     # a string or a list (run in order)
    depends_on: [generate]
    cwd: services/api
    env: {STAGE: dev}
    timeout: 10m

services:                      # highhx start / stop / restart / services
  api:
    command: uvicorn app:app --port 8000
    cwd: .
    env: {LOG_LEVEL: debug}
    port: 8000                  # checked for conflicts before starting
    health: {url: http://127.0.0.1:8000/health, timeout: 5s}
    depends_on: [db]
    ready_timeout: 30s

environment:
  default_profile: development

deploy:
  default: staging
  targets:
    <name>:
      type: local | docker | ssh | kubernetes | terraform | plugin:<provider>
      description: ""
      production: false         # production targets are CRITICAL risk
      require_branch: main
      require_clean: true       # also enforced by policies.require_clean_tree
      preflight: [pytest -q]    # commands that must pass first
      command: ./deploy.sh {{ version }}             # local/ssh/docker/kubernetes
                                # substituted values may only contain letters, digits and . _ + / : @ -
      rollback_command: ./deploy.sh {{ previous_version }}
      status_command: systemctl is-active app
      logs_command: journalctl -u app -n 200
      # ssh
      host: deploy@example.com
      port: 22
      user: deploy
      identity_file: ~/.ssh/deploy
      remote_dir: /srv/app
      # docker
      compose_file: compose.prod.yaml
      service: web
      image: registry/app        # versioned images enable automatic rollback
      build: true
      # kubernetes
      context: prod
      namespace: app
      manifests: k8s/            # kubectl apply -f / -k
      deployment: app            # rollout status / undo
      # terraform
      directory: infra/
      vars: {region: eu-west-1}
      health_check: {url: https://example.com/health, expected_status: 200, retries: 10, interval: 5s, timeout: 10s}
      auto_rollback: true        # roll back when the health check fails
      timeout: 15m
      env: {}
      settings: {}              # free-form, for plugin providers

database:
  url_env: DATABASE_URL        # read from the active environment profile
  migrations: {command: alembic upgrade head}   # or {directory: db/migrations}
  seed: {directory: db/seeds}
  backups_dir: .highhx/backups

release:
  tag_prefix: v
  changelog: CHANGELOG.md
  version_files: [pyproject.toml, src/app/__init__.py]   # auto-detected when omitted
  commit_message: "chore(release): {tag}"
  publish_command: uv publish
  push: false

security:
  ignore: ["docs/**"]           # globs skipped by scans
  allowlist: [3f2a9c1b04de]     # finding fingerprints to suppress
  max_file_size: 1000000
  scan_untracked: false         # default: only committed files

approvals:
  auto_approve: normal          # safe | normal | dangerous | critical
  yes_max_risk: critical
  typed_confirmation: critical
  non_bypassable: [publish, "deploy:production"]
  rules:
    - id: prod-psql
      pattern: "psql .*prod"
      risk: critical
      bypassable: false

plugins:
  enabled: []                   # empty = all installed
  disabled: []
  allow_code: false             # Python code plugins stay off until true
  index: [../plugin-index.json] # local JSON indexes or directories

watch:
  - {name: tests, paths: [src, tests], patterns: ["*.py"], run: pytest -x, debounce: 0.5}
schedules:
  - {name: nightly, cron: "0 2 * * *", workflow: ci}
triggers:
  push: [ci]
hooks:
  pre-commit: check             # workflow name or a command

workspace:
  members: ["apps/*", "packages/*"]

agent:                          # HighhX Pro agent (see agent.md)
  provider: highhx              # highhx | anthropic | openai | gemini
  model: claude-opus-5
  approval: ask                 # ask | auto-edit | read-only
  max_steps: 60                 # tool steps per request (capped by your plan)
  max_tokens: 32000             # output tokens per model response
  effort: high                  # low | medium | high | xhigh | max
  instructions: "Use pnpm, never npm."
  sync_sessions: true
```

Command strings are split without a shell unless they use shell syntax (pipes,
`&&`, redirects, globs, `$VAR`); then the platform shell is used (`/bin/sh -c` on
POSIX, `cmd /c` on Windows).

## Environment (`.highhx/environment.yaml`)

```yaml
default_profile: development
variables:
  DATABASE_URL: {required: true, secret: true, description: Primary database}
  PORT: {default: "8000", pattern: "^[0-9]+$"}
  LOG_LEVEL: {choices: [debug, info, warning]}
  SENTRY_DSN: {required: true, secret: true, profiles: [production]}
profiles:
  development: {files: [.env, .env.development], env: {DEBUG: "true"}}
  staging:     {files: [.env.staging]}
  production:  {files: [.env.production], protected: true}
```

Values of the active profile are injected into every command HighhX runs. Choose
the profile with `highhx env profile <name>` or `HIGHHX_ENV=<name>`.

## Policies (`.highhx/policies.yaml`)

See [security.md](security.md#policies).

## Config profiles

`.highhx/profiles/<name>.yaml` is deep-merged over `config.yaml` when you pass
`--config-profile <name>` or set `HIGHHX_PROFILE`. Create one with `highhx profile create ci`.

## Environment variables read by HighhX

| Variable | Effect |
|---|---|
| `HIGHHX_ENV` | Environment profile to use |
| `HIGHHX_PROFILE` | Config profile to apply |
| `HIGHHX_NON_INTERACTIVE` | Never prompt (deny instead) |
| `HIGHHX_DATA_DIR`, `HIGHHX_CONFIG_DIR`, `HIGHHX_CACHE_DIR` | Override user directories |
| `HIGHHX_ASCII` | ASCII status symbols and ASCII knight banner |
| `HIGHHX_BANNER` | Startup knight: `full`, `compact` or `off` (default: chosen from the terminal size) |
| `HIGHHX_TOKEN` | HighhX platform token (overrides `highhx login`) |
| `HIGHHX_API_URL` | HighhX platform URL (self-hosted / development) |
| `HIGHHX_BROWSER` | Browser binary for `highhx computer` (default: Chrome/Chromium/Edge/Brave) |
| `HIGHHX_HEADLESS` | Run the HighhX browser without a window (always on Linux without a display) |
| `HIGHHX_COMPUTER_TARGET` | The computer operated: `local` or `ssh://user@host` (see [COMPUTER_RUNTIME.md](COMPUTER_RUNTIME.md)) |
| `HIGHHX_VISION_PROVIDER`, `HIGHHX_VISION_BASE_URL`, `HIGHHX_VISION_MODEL`, `HIGHHX_VISION_COORDINATES`, `HIGHHX_VISION_FORMAT`, `HIGHHX_VISION_API_KEY` | The vision model for grounding and the vision operator ([PROVIDERS.md](PROVIDERS.md)) |
| `HIGHHX_PLANNER_BASE_URL`, `HIGHHX_PLANNER_MODEL`, `HIGHHX_PLANNER_API_KEY` | A local (OpenAI-compatible) model for `highhx agent loop --model`. A non-local endpoint needs `--remote-model` |
| `HIGHHX_ADB` | The adb binary for Android (default: `adb` on PATH, or `$ANDROID_HOME/platform-tools/adb`) |
| `HIGHHX_VAR_<NAME>` | A value for a browser workflow's secret-field variable at replay |
| `NO_COLOR` | Disable colors |

HighhX sets `HIGHHX_WORKFLOW`, `HIGHHX_EXECUTION_ID`, `HIGHHX_STEP_ID`,
`HIGHHX_OUTPUT` and `HIGHHX_PROJECT_ROOT` for workflow steps.
