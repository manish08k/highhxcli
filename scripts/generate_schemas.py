"""Regenerate schemas/*.json from the validation code (the single source of truth).

Usage: python scripts/generate_schemas.py
"""

from __future__ import annotations

import json
from pathlib import Path

from highhx.config.validation import CONFIG_SCHEMA
from highhx.plugins.manifest import MANIFEST_SCHEMA
from highhx.workflows.schema import workflow_json_schema

DRAFT = "https://json-schema.org/draft/2020-12/schema"


def documents() -> dict[str, dict[str, object]]:
    return {
        "config.schema.json": {
            "$schema": DRAFT,
            "$id": "https://highhx.dev/schemas/config.schema.json",
            "title": "HighhX project configuration (.highhx/config.yaml)",
            **CONFIG_SCHEMA.json_schema(),
        },
        "workflow.schema.json": {"$id": "https://highhx.dev/schemas/workflow.schema.json", **workflow_json_schema()},
        "plugin.schema.json": {
            "$schema": DRAFT,
            "$id": "https://highhx.dev/schemas/plugin.schema.json",
            "title": "HighhX plugin manifest (highhx-plugin.yaml)",
            **MANIFEST_SCHEMA.json_schema(),
        },
    }


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "schemas"
    for name, document in documents().items():
        (target / name).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {target / name}")
