# Application skills

A skill is checked, reusable knowledge about one application or site: where it applies (surfaces,
hosts, apps), what it needs, the catalog actions it uses, permission notes, runnable examples,
verification checks and known failure modes with their recovery.

Built in: `chrome`, `github`, `vscode`, `terminal`, `gmail`, `files`. A project adds or overrides skills
in `.highhx/skills/NAME.yaml`.

```text
highhx skills                 # each skill and whether it is usable here
highhx skills show github
highhx skills check           # exit 1 on unknown actions or malformed skills
highhx skills run files --example 1
```

Skills grant nothing: an example runs as a plan through the agent loop and executor (classified,
approved, verified, recorded), and the planner sees matching skills only as bounded data notes. No skill
contains or asks for credentials; sites are used through a HighhX browser profile the person signed in to.
