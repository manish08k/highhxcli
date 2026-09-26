# Plugins

A plugin is a directory with a `highhx-plugin.yaml` manifest
([schema](../schemas/plugin.schema.json)). It can contribute:

| Contribution | How | Needs code? |
|---|---|---|
| Commands | `contributes.commands` (runs a command) or `api.add_command(click_command)` | no / yes |
| Workflows | `contributes.workflows: [dir]` | no |
| Project templates | `contributes.templates: [dir]` (same layout as `templates/`) | no |
| Detectors | `contributes.detectors` (file globs) or `api.add_detector(fn)` | no / yes |
| Deployment backends | `api.add_deployment_strategy(type, factory)` / `api.add_cloud_provider(name, factory)` | yes |

## Manifest

```yaml
name: aws-deploy                 # letters, digits, - and _
version: 1.2.0
api_version: 1                   # plugin API version this plugin targets
description: Deploy to AWS with the aws CLI
author: Example Corp
entry: plugin.py:Plugin          # optional Python entry point
permissions: [commands, deploy]  # commands, subprocess, network, filesystem, env, deploy, detectors
contributes:
  commands:
    - name: aws-whoami
      description: Show the active AWS identity
      run: aws sts get-caller-identity
      risk: safe                 # safe | normal | dangerous | critical
  workflows: [workflows]
  templates: [templates]
  detectors:
    - name: serverless
      kind: framework
      files: [serverless.yml]
```

Declarative commands run through the engine (risk classification, approval, history)
with an isolated environment containing only safe variables, unless the plugin
declares the `env` permission.

## Code plugins

```python
# plugin.py
import click

from highhx.plugins.interface import HighhXPlugin, PluginAPI


class AWSProvider:
    name = "aws"

    def deploy(self, target, context):
        engine = context["engine"]
        # run commands through engine.run(...) so approvals and history apply
        return {"stack": target.settings["stack"]}

    def rollback(self, target, previous, context):
        return {"restored": previous.get("stack")}

    def status(self, target, context):
        return {"live": True}


class Plugin(HighhXPlugin):
    def register(self, api: PluginAPI) -> None:
        @click.command("aws-regions")
        def regions() -> None:
            click.echo("eu-west-1\nus-east-1")

        api.add_command(regions)
        api.add_cloud_provider("aws", AWSProvider)  # enables `type: plugin:aws`
```

Code runs only when **both** of the following are true:

1. `plugins.allow_code: true` in `.highhx/config.yaml`;
2. **you** trusted these exact plugin files. `highhx plugin install` records the SHA-256
   of the installed directory in your *user* trust store
   (`<user data dir>/highhx/trusted-plugins.json`); `highhx plugin trust NAME` does the
   same for a plugin that arrived with the repository (e.g. committed by a teammate).
   Any change to the files blocks the code again until you trust it anew.

Trust deliberately lives outside the project: `.highhx/config.yaml` and
`.highhx/plugins.lock.json` come with the repository, so they must not be able to
switch plugin code on by themselves. Plugins containing symbolic links are rejected.

Python code cannot be sandboxed reliably inside one interpreter, so HighhX does not
pretend to: it gates *whether* code runs (explicit opt-in, per-user content trust) and
each contribution checks the plugin's declared permissions. Declarative plugin commands
run with an isolated environment: they never receive your environment-profile values
(the contents of `.env` files) unless the plugin declares the `env` permission.

## Managing plugins

```bash
highhx plugin install ./path/to/plugin          # local directory
highhx plugin install https://github.com/org/highhx-aws.git   # git clone (network)
highhx plugin install aws-deploy                # name from plugins.index
highhx plugin install ./plugin --global         # for your user, all projects
highhx plugin list
highhx plugin update aws-deploy                 # reinstall from the recorded source
highhx plugin remove aws-deploy
highhx plugin trust aws-deploy                  # allow the plugin's current code to run
highhx plugin search aws                        # searches plugins.index (offline)
```

A plugin index is a JSON file (or a directory of plugins):

```json
{"plugins": [{"name": "aws-deploy", "description": "Deploy to AWS", "source": "https://github.com/org/highhx-aws.git", "version": "1.2.0"}]}
```
