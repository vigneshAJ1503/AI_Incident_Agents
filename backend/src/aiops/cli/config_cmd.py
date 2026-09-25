"""`aiops config ...` commands."""

from __future__ import annotations

import json

import typer
from rich.table import Table

from aiops.cli.common import EnvOption, console, handle_errors
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import load_settings

app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)


@app.command("validate")
@handle_errors
def validate(env: str | None = EnvOption) -> None:
    """Validate the environment config and its service catalog."""
    settings = load_settings(env)
    catalog = ServiceCatalog.from_settings(settings)
    table = Table(title=f"Environment '{settings.environment}' is valid", show_header=True)
    table.add_column("Capability")
    table.add_column("Provider")
    table.add_column("MCP")
    table.add_column("Allowed tools")
    for name, cap in sorted(settings.capabilities.items()):
        target = cap.mcp.url if cap.mcp.transport == "http" else cap.mcp.command
        table.add_row(
            name, cap.provider, f"{cap.mcp.transport} {target}", ", ".join(cap.tool_allowlist)
        )
    console.print(table)
    console.print(
        f"LLM: {settings.llm.provider} · models: {dict(settings.llm.models) or 'not set'} · "
        f"api key: {'set' if settings.llm.api_key else 'not set'}"
    )
    console.print(
        f"Service catalog: {len(catalog.services)} services, environments {catalog.environments}"
    )


@app.command("show")
@handle_errors
def show(env: str | None = EnvOption) -> None:
    """Print the resolved configuration as JSON (secrets masked)."""
    settings = load_settings(env)
    console.print_json(json.dumps(settings.safe_dump()))
