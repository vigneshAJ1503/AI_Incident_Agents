"""`aiops prompts ...` commands."""

from __future__ import annotations

import typer
from rich.table import Table

from aiops.cli.common import EnvOption, console, handle_errors
from aiops.core.config import find_config_dir, load_settings_lenient
from aiops.core.prompts import PromptLoader

app = typer.Typer(help="Versioned prompts.", no_args_is_help=True)


def _loader(profile: str | None = None) -> PromptLoader:
    """Shared config/prompts, overlaid with the profile's prompts/ folder (if any)."""
    config_dir = find_config_dir()
    settings, _ = load_settings_lenient(profile, config_dir, keep_missing=True)
    return PromptLoader(config_dir / "prompts", overrides=settings.prompt_override_dirs())


@app.command("list")
@handle_errors
def list_prompts(env: str | None = EnvOption) -> None:
    """List prompts and their versions (with the profile's overrides)."""
    loader = _loader(env)
    table = Table("Prompt", "Versions", "Latest ref", "Source")
    for name in loader.names():
        latest = loader.load(name)
        source = loader.source(name, latest.version)
        where = "shared" if source.is_relative_to(find_config_dir()) else "profile"
        table.add_row(name, ", ".join(loader.versions(name)), latest.ref, where)
    console.print(table)


@app.command("show")
@handle_errors
def show(
    name: str,
    version: str | None = typer.Option(None, help="e.g. v1 (default: latest)"),
    env: str | None = EnvOption,
) -> None:
    """Print a prompt."""
    prompt = _loader(env).load(name, version)
    console.print(f"[bold]{prompt.ref}[/bold]\n")
    console.print(prompt.text, markup=False)
