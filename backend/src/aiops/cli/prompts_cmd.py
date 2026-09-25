"""`aiops prompts ...` commands."""

from __future__ import annotations

import typer
from rich.table import Table

from aiops.cli.common import console, handle_errors
from aiops.core.config import find_config_dir
from aiops.core.prompts import PromptLoader

app = typer.Typer(help="Versioned prompts.", no_args_is_help=True)


def _loader() -> PromptLoader:
    return PromptLoader(find_config_dir() / "prompts")


@app.command("list")
@handle_errors
def list_prompts() -> None:
    """List prompts and their versions."""
    loader = _loader()
    table = Table("Prompt", "Versions", "Latest ref")
    for name in loader.names():
        table.add_row(name, ", ".join(loader.versions(name)), loader.load(name).ref)
    console.print(table)


@app.command("show")
@handle_errors
def show(
    name: str, version: str | None = typer.Option(None, help="e.g. v1 (default: latest)")
) -> None:
    """Print a prompt."""
    prompt = _loader().load(name, version)
    console.print(f"[bold]{prompt.ref}[/bold]\n")
    console.print(prompt.text, markup=False)
