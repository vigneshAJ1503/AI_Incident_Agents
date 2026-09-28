"""`aiops profile ...`: company profiles (ADR-0011, docs/portability.md)."""

from __future__ import annotations

import json

import typer
from rich.markup import escape
from rich.table import Table

from aiops.cli.common import console, err_console, handle_errors
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import (
    CATALOG_FILE,
    PROFILE_FILE,
    PROFILE_VAR,
    find_config_dir,
    find_profiles_dir,
    list_profile_names,
    load_settings_lenient,
    load_yaml,
    selected_profile,
)
from aiops.core.profiles import (
    PROVIDERS,
    ValidationReport,
    catalog_summary,
    diff_dumps,
    init_profile,
    profile_env_vars,
    resolved_dump,
    validate_profile,
)

app = typer.Typer(help="Company profiles: one folder per company.", no_args_is_help=True)

ProfileArg = typer.Argument(None, help="Profile name (default: $AIOPS_PROFILE or 'local').")


@app.command("list")
@handle_errors
def list_profiles() -> None:
    """List the profiles in profiles/ (or $AIOPS_PROFILES_DIR)."""
    config_dir = find_config_dir()
    profiles_dir = find_profiles_dir(config_dir)
    current = selected_profile(None)
    table = Table("", "Profile", "Company", "Extends", "Catalog", "Description")
    for name in list_profile_names(profiles_dir):
        data = load_yaml(profiles_dir / name / PROFILE_FILE)
        meta = data.get("metadata") or {}
        has_catalog = (profiles_dir / name / CATALOG_FILE).is_file()
        table.add_row(
            "*" if name == current else "",
            name + (" (template)" if name.startswith("_") else ""),
            str(meta.get("company", "")),
            str(data.get("extends", "")),
            "own" if has_catalog else "inherited",
            str(meta.get("description", "")).strip()[:70],
        )
    console.print(table)
    console.print(f"Profiles dir: {profiles_dir}  ·  selected: {current} (* )")


@app.command("show")
@handle_errors
def show(
    name: str | None = ProfileArg,
    resolved: bool = typer.Option(
        False, "--resolved", help="Print the merged configuration as JSON (secrets masked)."
    ),
) -> None:
    """Summarize a profile, or print it fully resolved with --resolved."""
    settings, missing = load_settings_lenient(name, keep_missing=True)
    if resolved:
        console.print_json(json.dumps(resolved_dump(settings)))
        if missing:
            err_console.print(f"[yellow]Unset required variables:[/yellow] {', '.join(missing)}")
        return
    meta = settings.metadata
    console.print(f"[bold]Profile {settings.profile}[/bold]  {meta.company}")
    if meta.description:
        console.print(meta.description)
    if meta.owners:
        console.print(f"Owners: {', '.join(meta.owners)}")
    if settings.profile_chain:
        chain = " -> ".join(p.name for p in settings.profile_chain)
        console.print(f"Folder: {settings.profile_dir}  ·  extends chain: {chain}")
    else:
        console.print("[yellow]Legacy config/environments/ file (migrate to profiles/).[/yellow]")
    console.print(f"Service catalog: {settings.catalog_path()}")
    overrides = settings.prompt_override_dirs()
    console.print(f"Prompt overrides: {', '.join(map(str, overrides)) or 'none (shared prompts)'}")
    table = Table("Capability", "Provider", "Status", "MCP", "Allowed tools")
    for cap_name, cap in sorted(settings.capabilities.items()):
        spec = PROVIDERS.get(cap_name, {}).get(cap.provider)
        status = spec.status if spec else "unknown"
        target = cap.mcp.url if cap.mcp.transport == "http" else cap.mcp.command
        table.add_row(
            cap_name + ("" if cap.enabled else " (disabled)"),
            cap.provider,
            status,
            str(target),
            ", ".join(cap.tool_allowlist),
        )
    console.print(table)
    console.print(
        f"LLM: {settings.llm.provider} · models: {dict(settings.llm.models) or 'not set'}"
    )
    if settings.profile_dir is not None:
        env_vars = profile_env_vars(settings.profile_dir)
        required = sorted(n for n, req in env_vars.items() if req)
        console.print(f"Required variables: {', '.join(required) or 'none'}")
    if missing:
        console.print(f"[yellow]Unset required variables:[/yellow] {', '.join(missing)}")


def _validate_one(name: str) -> ValidationReport:
    return validate_profile(name)


@app.command("validate")
@handle_errors
def validate(
    name: str | None = ProfileArg,
    all_profiles: bool = typer.Option(False, "--all", help="Validate every profile."),
    strict: bool = typer.Option(False, "--strict", help="Warnings fail too."),
) -> None:
    """Validate a profile: schema, providers, required settings/variables, catalog."""
    if all_profiles:
        config_dir = find_config_dir()
        names = [
            n for n in list_profile_names(find_profiles_dir(config_dir)) if not n.startswith("_")
        ]
    else:
        names = [selected_profile(name)]
    failed = False
    for profile in names:
        report = _validate_one(profile)
        bad = bool(report.errors) or (strict and bool(report.warnings))
        failed |= bad
        mark = "[red]invalid[/red]" if bad else "[green]valid[/green]"
        console.print(
            f"Profile '{profile}': {mark} ({len(report.errors)} errors, "
            f"{len(report.warnings)} warnings)"
        )
        for issue in report.issues:
            color = "red" if issue.level == "error" else "yellow"
            console.print(f"  [{color}]{issue.level}[/{color}]: {escape(issue.message)}")
    if failed:
        raise typer.Exit(code=1)


@app.command("init")
@handle_errors
def init(
    name: str = typer.Argument(..., help="New profile name, e.g. 'acme'."),
    source: str = typer.Option(
        "_template", "--from", help="Profile to copy: _template (documented) or local."
    ),
    company: str | None = typer.Option(None, help="metadata.company (default: the name)."),
    description: str | None = typer.Option(None, help="metadata.description."),
) -> None:
    """Create profiles/<name> from the template (or another profile) and print next steps."""
    config_dir = find_config_dir()
    profiles_dir = find_profiles_dir(config_dir)
    target = init_profile(profiles_dir, name, source, company=company, description=description)
    console.print(f"[green]Created profile '{name}'[/green] in {target} (from {source})")
    console.print(
        "\n[bold]Next steps[/bold] (docs/portability.md, day-1 checklist):\n"
        f"  1. Edit {target / PROFILE_FILE}: providers, MCP URLs, field/label mappings, links.\n"
        f"  2. {target / CATALOG_FILE}: generate it with  aiops catalog import --profile {name} "
        "--from kubernetes|backstage --dry-run  (then review and hand-edit).\n"
        f"  3. cp {target / '.env.example'} {target / '.env'}  and fill in the secrets "
        "(.env is gitignored; read-only credentials only).\n"
        f"  4. uv run aiops profile validate {name}\n"
        f"  5. export {PROFILE_VAR}={name}   (or pass --profile {name})\n"
        f"  6. uv run aiops doctor --profile {name}   checks every connection, tool and catalog "
        "identifier.\n"
        "\n[yellow]This repository is public:[/yellow] company profiles are gitignored by "
        "default. Keep yours in a private repo and point AIOPS_PROFILES_DIR at it."
    )


@app.command("diff")
@handle_errors
def diff(
    a: str = typer.Argument(..., help="First profile."),
    b: str = typer.Argument(..., help="Second profile."),
) -> None:
    """Show every resolved setting and catalog identifier that differs (secrets masked)."""
    dumps = []
    for name in (a, b):
        settings, _ = load_settings_lenient(name, keep_missing=True)
        dump = resolved_dump(settings)
        dump.pop("environment", None)
        dump["catalog"] = catalog_summary(ServiceCatalog.from_settings(settings))
        dumps.append(dump)
    rows = diff_dumps(dumps[0], dumps[1])
    if not rows:
        console.print(f"Profiles '{a}' and '{b}' resolve to the same configuration.")
        return
    table = Table("Key", a, b, title=f"{len(rows)} differences")
    for key, va, vb in rows:
        table.add_row(key, _fmt(va), _fmt(vb))
    console.print(table)


def _fmt(value: object) -> str:
    if value is None:
        return "[dim]-[/dim]"
    return json.dumps(value) if not isinstance(value, str) else value
