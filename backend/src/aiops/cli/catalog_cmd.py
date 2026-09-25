"""`aiops catalog ...` commands."""

from __future__ import annotations

import json

import typer
from rich.table import Table

from aiops.cli.common import EnvOption, console, err_console, handle_errors
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import load_settings

app = typer.Typer(help="Query the service catalog.", no_args_is_help=True)


@app.command("list")
@handle_errors
def list_services(env: str | None = EnvOption) -> None:
    """List services in the catalog."""
    catalog = ServiceCatalog.from_settings(load_settings(env))
    table = Table("Service", "Aliases", "Owner team", "Depends on")
    for s in catalog.services:
        table.add_row(
            s.name, ", ".join(s.aliases), s.owners.get("team", ""), ", ".join(s.depends_on)
        )
    console.print(table)


@app.command("resolve")
@handle_errors
def resolve(
    text: str = typer.Argument(..., help="Free text, e.g. 'payments api'."),
    environment: str | None = typer.Option(None, "--environment", "-E", help="e.g. prod"),
    env: str | None = EnvOption,
) -> None:
    """Resolve free text to a catalog service and show its identifiers."""
    catalog = ServiceCatalog.from_settings(load_settings(env))
    result = catalog.resolve(text)
    if result.service is None:
        if result.ambiguous:
            err_console.print(f"[yellow]Ambiguous:[/yellow] '{text}' matches {result.candidates}")
        else:
            err_console.print(f"[red]No service matches[/red] '{text}'")
        raise typer.Exit(code=1)
    env_name = catalog.resolve_environment(environment)
    if environment and env_name is None:
        err_console.print(
            f"[red]Unknown environment[/red] '{environment}' (known: {catalog.environments})"
        )
        raise typer.Exit(code=1)
    service = result.service
    identifiers = {cap: service.identifiers(cap, env_name) for cap in sorted(service.capabilities)}
    console.print(f"[green]{service.name}[/green] (match: {result.match}, environment: {env_name})")
    console.print_json(json.dumps(identifiers))
