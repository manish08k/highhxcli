"""The published JSON schemas and the examples must stay in sync with the code."""

import json
from pathlib import Path

import pytest
import yaml

from highhx.config.validation import CONFIG_SCHEMA, validate_config
from highhx.environment.variables import ENVIRONMENT_SCHEMA
from highhx.plugins.manifest import MANIFEST_SCHEMA
from highhx.policy.validator import validate_policies
from highhx.workflows.loader import WorkflowLoader
from highhx.workflows.schema import workflow_json_schema
from highhx.workflows.validator import validate_file

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("config.schema.json", CONFIG_SCHEMA.json_schema()),
        ("workflow.schema.json", workflow_json_schema()),
        ("plugin.schema.json", MANIFEST_SCHEMA.json_schema()),
    ],
)
def test_published_schema_matches_code(name: str, expected: dict) -> None:
    """Regenerate with `python scripts/generate_schemas.py` when this fails."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("generate_schemas", ROOT / "scripts" / "generate_schemas.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    published = json.loads((ROOT / "schemas" / name).read_text())
    assert published == json.loads(json.dumps(module.documents()[name]))
    assert published["properties"] == expected["properties"]


EXAMPLES = sorted(p for p in (ROOT / "examples").iterdir() if (p / ".highhx").is_dir())


def test_examples_exist() -> None:
    assert {p.name for p in EXAMPLES} >= {
        "basic",
        "python",
        "node",
        "flutter",
        "docker",
        "monorepo",
        "ci",
        "production",
    }


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_example_configuration_is_valid(example: Path) -> None:
    highhx_dir = example / ".highhx"
    assert validate_config(yaml.safe_load((highhx_dir / "config.yaml").read_text())) == []
    if (highhx_dir / "environment.yaml").exists():
        assert ENVIRONMENT_SCHEMA.validate(yaml.safe_load((highhx_dir / "environment.yaml").read_text()), "") == []
    if (highhx_dir / "policies.yaml").exists():
        assert validate_policies(yaml.safe_load((highhx_dir / "policies.yaml").read_text())) == []
    loader = WorkflowLoader.for_project(highhx_dir / "workflows")
    for ref in loader.list():
        report = validate_file(ref.path, loader=loader, check_tools=False)
        assert report.ok, (ref.path, report.errors)
