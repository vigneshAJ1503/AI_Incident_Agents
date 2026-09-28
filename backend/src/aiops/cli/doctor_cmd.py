"""`aiops doctor`: check a profile end to end (config, MCP servers, catalog, LLM)."""

from __future__ import annotations

import asyncio
import json

import typer
from rich.markup import escape
from rich.table import Table

from aiops.cli.common import EnvOption, console, handle_errors
from aiops.core.config import load_settings_lenient
from aiops.core.doctor import Doctor, DoctorOptions, DoctorReport, Status

_STYLE = {Status.OK: "green", Status.WARN: "yellow", Status.FAIL: "red", Status.SKIP: "dim"}


def render(report: DoctorReport) -> Table:
    table = Table(
        "Capability", "Check", "Status", "Detail", "Fix", title=f"aiops doctor · {report.profile}"
    )
    for c in report.checks:
        latency = f" ({c.latency_ms:.0f} ms)" if c.latency_ms is not None else ""
        style = _STYLE[c.status]
        table.add_row(
            c.capability,
            c.check,
            f"[{style}]{c.status.value}[/{style}]",
            escape(c.detail) + latency,
            escape(c.hint),
        )
    return table


@handle_errors
def doctor(
    env: str | None = EnvOption,
    capability: list[str] = typer.Option(
        [], "--capability", "-c", help="Only these capabilities (repeatable). Default: all."
    ),
    service: list[str] = typer.Option(
        [], "--service", "-s", help="Catalog services to verify (repeatable). Default: a sample."
    ),
    sample: int = typer.Option(3, help="How many catalog services to sample per capability."),
    environment: str = typer.Option(
        "production", "--environment", "-E", help="Catalog environment for identifiers."
    ),
    timeout: float = typer.Option(10.0, help="Seconds per connect and per smoke call."),
    skip_llm: bool = typer.Option(False, "--skip-llm", help="Don't ping the LLM."),
    strict: bool = typer.Option(False, "--strict", help="Exit 1 on warnings."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Is this profile ready? Config, MCP reachability, tool contract, smoke queries, LLM.

    Exit code: 0 all OK, 1 warnings (with --strict), 2 at least one FAIL. Read-only: every
    call goes through the capability's allowlist; secret values are never printed.
    """
    settings, missing = load_settings_lenient(env, keep_missing=True)
    options = DoctorOptions(
        capabilities=tuple(capability),
        environment=environment,
        sample=sample,
        services=tuple(service),
        timeout_s=timeout,
        skip_llm=skip_llm,
    )
    report = asyncio.run(Doctor(settings, options, missing_vars=missing).run())
    code = report.exit_code(strict=strict)
    if as_json:
        console.print_json(json.dumps(report.as_dict(strict=strict)))
    else:
        console.print(render(report))
        counts = ", ".join(f"{report.count(s)} {s.value}" for s in Status)
        verdict = {0: "[green]ready[/green]", 1: "[yellow]warnings[/yellow]"}.get(
            code, "[red]not ready[/red]"
        )
        console.print(f"{verdict}: {counts} (exit {code})")
    if code:
        raise typer.Exit(code=code)
