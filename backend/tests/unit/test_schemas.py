from __future__ import annotations

from pathlib import Path

from aiops.core.schemas import render_schemas

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "docs" / "schemas"


def test_committed_schemas_are_up_to_date() -> None:
    """If this fails, run: cd backend && uv run aiops schemas export"""
    rendered = render_schemas()
    committed = {p.name: p.read_text() for p in SCHEMA_DIR.glob("*.schema.json")}
    assert set(committed) == set(rendered), "schema files added/removed — re-export"
    stale = [name for name, content in rendered.items() if committed[name] != content]
    assert not stale, f"stale schemas: {stale} — run `uv run aiops schemas export`"
