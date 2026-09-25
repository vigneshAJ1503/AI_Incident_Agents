"""JSON Schema export of the domain models (docs/schemas/*.schema.json)."""

from __future__ import annotations

import json
from pathlib import Path

from aiops.core.models import SCHEMA_MODELS


def render_schemas() -> dict[str, str]:
    """File name -> pretty JSON schema, deterministic output."""
    return {
        f"{model.__name__}.schema.json": json.dumps(
            model.model_json_schema(), indent=2, sort_keys=True
        )
        + "\n"
        for model in SCHEMA_MODELS
    }


def export_schemas(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in render_schemas().items():
        path = out_dir / name
        path.write_text(content)
        written.append(path)
    return written
