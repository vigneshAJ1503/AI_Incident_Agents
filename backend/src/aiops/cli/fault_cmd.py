"""`aiops fault ...`: inject reproducible incidents into the local Kubernetes sample system."""

from __future__ import annotations

import os
import subprocess

import typer
from rich.table import Table

from aiops.cli.common import console, err_console
from aiops.core.config import find_config_dir
from aiops.faults import FAULT_TYPES, FAULTS, FaultError, FaultInjector, FaultState, kubectl_runner

app = typer.Typer(
    help="Inject and revert incidents in the local cluster (one at a time).", no_args_is_help=True
)

ContextOption = typer.Option("aiops", "--context", help="kubectl context (Minikube profile).")


def _injector(context: str) -> FaultInjector:
    return FaultInjector(find_config_dir().parent, kubectl_runner(context), log=console.print)


@app.command("list")
def list_faults() -> None:
    """Show available faults."""
    aliases: dict[str, list[str]] = {}
    for alias, scenario in FAULT_TYPES.items():
        aliases.setdefault(scenario, []).append(alias)
    table = Table("Scenario", "Service", "Aliases", "What happens")
    for fault in FAULTS.values():
        table.add_row(
            fault.scenario,
            fault.service,
            ", ".join(aliases.get(fault.scenario, [])),
            fault.description,
        )
    console.print(table)


@app.command("inject")
def inject(
    fault: str = typer.Argument(
        ..., help="Scenario (S1..S5) or type (db-timeout, memory-leak, ...)."
    ),
    no_wait: bool = typer.Option(
        False, "--no-wait", help="Don't wait for the incident to develop."
    ),
    context: str = ContextOption,
) -> None:
    """Inject one fault and leave it active (revert with `aiops fault revert`)."""
    try:
        state = _injector(context).inject(fault, wait=not no_wait)
    except FaultError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[yellow]{state.scenario} is ACTIVE[/yellow] since {state.injected_at}. Revert: make revert-fault"
    )


@app.command("revert")
def revert(context: str = ContextOption) -> None:
    """Restore the healthy baseline (re-applies deploy/k8s/base)."""
    try:
        _injector(context).revert()
    except FaultError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print("[green]Healthy baseline restored.[/green]")


@app.command("status")
def status(context: str = ContextOption) -> None:
    """Which scenario (if any) is active."""
    state = _injector(context).state()
    if state.scenario:
        console.print(f"[yellow]{state.scenario} active[/yellow] since {state.injected_at}")
    else:
        console.print("[green]No fault active (healthy baseline).[/green]")


@app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run(
    ctx: typer.Context,
    fault: str = typer.Argument(..., help="Scenario (S1..S5) or type."),
    context: str = ContextOption,
) -> None:
    """Inject, wait, run a command (after --), then ALWAYS revert. Holds the cluster lock.

    The command gets AIOPS_SCENARIO and AIOPS_INCIDENT_START in its environment.
    Example: aiops fault run S1 -- uv run python -m tests.fixtures.record_k8s
    """
    command = list(ctx.args)
    exit_code = 0

    def action(state: FaultState) -> None:
        nonlocal exit_code
        if not command:
            console.print("(no command given; the fault was live for its settle time)")
            return
        env = {
            **os.environ,
            "AIOPS_SCENARIO": state.scenario or "",
            "AIOPS_INCIDENT_START": state.injected_at or "",
        }
        console.print(f"  - running: {' '.join(command)}")
        exit_code = subprocess.run(command, env=env, check=False).returncode  # noqa: S603 - user-supplied dev command

    try:
        _injector(context).run_scenario(fault, action)
    except FaultError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    raise typer.Exit(code=exit_code)
