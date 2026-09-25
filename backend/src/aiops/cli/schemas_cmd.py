"""`aiops schemas ...` commands."""

from __future__ import annotations

from pathlib import Path

import typer

from aiops.cli.common import console
from aiops.core.config import find_config_dir
from aiops.core.schemas import export_schemas

app = typer.Typer(help="Domain model JSON schemas.", no_args_is_help=True)


@app.command("export")
def export(
    out: Path | None = typer.Option(None, help="Output directory (default: <repo>/docs/schemas)."),
) -> None:
    """Write JSON schemas for all domain models."""
    target = out or find_config_dir().parent / "docs" / "schemas"
    for path in export_schemas(target):
        console.print(f"wrote {path}")
