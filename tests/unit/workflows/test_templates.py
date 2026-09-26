from highhx.workflows.templates import TemplateLibrary, prune_workflow, render


def test_render_quotes_whole_values_and_drops_empty_lines() -> None:
    text = render(
        "a: {{ commands.test }}\nb: {{ commands.missing }}\nc: run {{ project.name }} now\n# {{ keep }}\n",
        {"commands": {"test": 'pytest -k "x"'}, "project": {"name": "demo"}},
    )
    assert 'a: "pytest -k \\"x\\""' in text
    assert "b:" not in text
    assert "c: run demo now" in text
    assert "# {{ keep }}" in text


def test_prune_rewires_dependencies() -> None:
    text = "name: x\nsteps:\n  - id: a\n    run: echo\n  - id: b\n    depends_on: [a]\n  - id: c\n    run: echo\n    depends_on: [b]\n"
    pruned = prune_workflow(text)
    assert pruned is not None and "id: b" not in pruned and "- a" in pruned


def test_prune_returns_none_without_steps() -> None:
    assert prune_workflow("name: x\nsteps:\n  - id: a\n") is None


def test_every_stack_renders_valid_workflows() -> None:
    import yaml

    from highhx.workflows.parser import parse_workflow

    library = TemplateLibrary()
    context = {
        "project": {"name": "demo", "type": "python"},
        "commands": {
            k: f"echo {k}"
            for k in (
                "dev",
                "start",
                "test",
                "build",
                "lint",
                "format",
                "fix",
                "typecheck",
                "install",
                "package",
                "clean",
            )
        },
        "workspace": {"members": ["packages/*"]},
    }
    for stack in library.stacks():
        rendered = library.render_project(stack, context)
        assert "config.yaml" in rendered.files
        for name, content in rendered.files.items():
            if name.startswith("workflows/"):
                parse_workflow(yaml.safe_load(content))
