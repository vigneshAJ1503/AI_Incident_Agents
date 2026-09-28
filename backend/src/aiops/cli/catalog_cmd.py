"""`aiops catalog ...` commands."""

from __future__ import annotations

import asyncio
import json
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from aiops.cli.common import EnvOption, console, err_console, handle_errors
from aiops.core.catalog import ServiceCatalog, normalize
from aiops.core.catalog_import import (
    MappingOptions,
    apply_plan,
    backstage_token,
    check_names,
    fetch_backstage_api,
    fetch_k8s_deployments,
    load_backstage_dir,
    plan_import,
    render_plan,
    resolved_catalog_data,
    service_from_backstage,
    service_from_deployment,
    target_catalog,
    validate_catalog_text,
)
from aiops.core.config import ConfigError, load_settings
from aiops.mcp.client import MCPClientError
from aiops.mcp.registry import MCPRegistry

app = typer.Typer(
    help="Query the service catalog, or import it from Kubernetes/Backstage.",
    no_args_is_help=True,
)


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


class ImportSource(StrEnum):
    kubernetes = "kubernetes"
    backstage = "backstage"


@app.command("import")
@handle_errors
def import_catalog(
    source: ImportSource = typer.Option(..., "--from", help="kubernetes | backstage"),
    env: str | None = EnvOption,
    namespace: list[str] = typer.Option(
        [], "--namespace", "-n", help="kubernetes: namespaces to read (repeatable)."
    ),
    selector: str | None = typer.Option(
        None, "--selector", "-l", help="kubernetes: label selector, e.g. 'team!=platform'."
    ),
    exclude: list[str] = typer.Option([], "--exclude", "-x", help="Service names to skip."),
    path: Path | None = typer.Option(
        None, "--path", help="backstage: a catalog-info.yaml file or a directory of them."
    ),
    url: str | None = typer.Option(None, "--url", help="backstage: base URL of the API."),
    token_env: str = typer.Option(
        "BACKSTAGE_TOKEN", "--token-env", help="backstage: env var holding the API token."
    ),
    environment: str | None = typer.Option(
        None,
        "--environment",
        "-E",
        help="Catalog environment for per-namespace identifiers (default: the namespace, "
        "resolved through the catalog's environment aliases, e.g. prod -> production).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the diff, write nothing."),
    replace: bool = typer.Option(
        False, "--replace/--merge", help="--merge (default) never overwrites hand edits."
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Write here instead of the profile's services.yaml."
    ),
    limit: int = typer.Option(200, help="kubernetes: max deployments per namespace."),
) -> None:
    """Generate or update services.yaml from Kubernetes labels or Backstage (read-only).

    Default --merge only ADDS services and identifiers and never overwrites a value that is
    already in the catalog; conflicts are listed as 'kept'. Always review the diff.
    """
    settings = load_settings(env)
    try:
        existing_catalog: ServiceCatalog | None = ServiceCatalog.from_settings(settings)
    except ConfigError:
        existing_catalog = None
    target, extends = target_catalog(settings)
    if output is not None:
        target, extends = output, None

    def env_for(ns: str | None) -> str | None:
        if environment:
            return environment
        if ns is None:
            return None
        resolved = existing_catalog.resolve_environment(ns) if existing_catalog else None
        return resolved or ns

    if source is ImportSource.kubernetes:
        namespaces = list(namespace)
        if not namespaces:
            default_ns = settings.capability("k8s").settings.get("default_namespace")
            if not default_ns:
                raise typer.BadParameter("give --namespace (no k8s default_namespace set)")
            namespaces = [str(default_ns)]

        async def fetch() -> list[dict[str, Any]]:
            registry = MCPRegistry(settings)
            async with registry.toolset("k8s", agent="catalog-import") as toolset:
                return await fetch_k8s_deployments(
                    toolset, namespaces, label_selector=selector, limit=limit
                )

        try:
            deployments = asyncio.run(fetch())
        except MCPClientError as exc:
            err_console.print(f"[red]MCP error:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        imported = [
            service_from_deployment(
                d, MappingOptions.from_settings(settings, env_for(d.get("namespace")))
            )
            for d in deployments
        ]
    else:
        if path is None and url is None:
            raise typer.BadParameter("backstage needs --path DIR|FILE or --url URL")
        entities = load_backstage_dir(path) if path is not None else []
        if url is not None:
            entities += fetch_backstage_api(url, backstage_token(token_env))
        opts = MappingOptions.from_settings(settings, env_for(None))
        imported = [service_from_backstage(e, opts) for e in entities]

    skip = {normalize(x) for x in exclude}
    imported = [s for s in imported if normalize(s["name"]) not in skip]
    odd = check_names(imported)
    if odd:
        err_console.print(f"[yellow]Unusual service names (check them):[/yellow] {odd}")
    existing = resolved_catalog_data(output if output is not None else settings.catalog_path())
    plan = plan_import(existing, imported, mode="replace" if replace else "merge")
    console.print(f"[bold]catalog import --from {source.value}[/bold] -> {target}")
    console.print(
        f"{len(imported)} services found: {plan.count('new')} new, "
        f"{plan.count('update')} updated, {plan.count('unchanged')} unchanged, "
        f"{plan.conflicts} hand-edited values kept"
    )
    for line in render_plan(plan):
        style = {"+": "green", "~": "yellow", "=": "cyan"}.get(line.strip()[:1], "dim")
        console.print(f"[{style}]{escape(line)}[/{style}]")
    if not plan.has_changes:
        console.print("Nothing to change.")
        return
    text = apply_plan(target, plan, extends=extends)
    validate_catalog_text(target, text)  # never write a catalog that doesn't load
    if dry_run:
        console.print("[dim]--dry-run: nothing written.[/dim]")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    console.print(
        f"[green]Wrote {target}[/green]. Review it (git diff), then: aiops profile validate "
        f"{settings.profile} && aiops doctor --profile {settings.profile}"
    )
