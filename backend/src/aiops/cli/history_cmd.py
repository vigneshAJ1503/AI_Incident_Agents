"""`aiops history`, `aiops show <id>` and `aiops db upgrade` (the evidence store, PR-032)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from typing import Any

import typer
from rich.table import Table

from aiops.cli.common import EnvOption, console, err_console, handle_errors
from aiops.core.config import Settings, load_settings
from aiops.core.events import EventBus
from aiops.core.models import Investigation
from aiops.store.db import StoreError, database_url
from aiops.store.repository import InvestigationStore

app = typer.Typer(
    help="The evidence store (Postgres schema 'investigations').", no_args_is_help=True
)


def open_store(settings: Settings) -> InvestigationStore:
    """A migrated store, or exit 2 with a readable message."""
    store = InvestigationStore.from_settings(settings)
    try:
        store.migrate()
    except StoreError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    return store


def run[T](coro_factory: Coroutine[Any, Any, T], store: InvestigationStore) -> T:
    async def _run() -> T:
        try:
            return await coro_factory
        finally:
            await store.close()

    return asyncio.run(_run())


async def persist(store: InvestigationStore, inv: Investigation, bus: EventBus) -> None:
    await store.save(inv)
    await store.save_events(bus.history(inv.id))


@app.command("upgrade")
@handle_errors
def upgrade_cmd(env: str | None = EnvOption) -> None:
    """Create/upgrade the store's tables (Alembic upgrade head)."""
    settings = load_settings(env)
    open_store(settings)
    where = database_url(settings).split("@")[-1]
    console.print(f"[green]Store ready[/green] at {where} (schema {settings.storage.db_schema})")


@handle_errors
def history(
    limit: int = typer.Option(20, "--limit", "-n", help="How many (newest first)."),
    status: str | None = typer.Option(None, "--status"),
    service: str | None = typer.Option(None, "--service", "-s"),
    mode: str | None = typer.Option(None, "--mode", help="live | replay | demo"),
    as_json: bool = typer.Option(False, "--json"),
    env: str | None = EnvOption,
) -> None:
    """List stored investigations, newest first."""
    store = open_store(load_settings(env))
    items = run(store.search(status=status, service=service, mode=mode, limit=limit), store)
    if as_json:
        from aiops.store.repository import summary_of

        console.print_json(json.dumps([summary_of(i) for i in items]))
        return
    table = Table(
        "Id", "Created (UTC)", "Service", "Status", "Severity", "Conf.", "Mode", "Summary"
    )
    for inv in items:
        report = inv.report
        table.add_row(
            inv.id,
            f"{inv.created_at:%Y-%m-%d %H:%M}",
            (inv.context.service if inv.context else None) or "-",
            inv.status.value,
            report.severity if report else "-",
            f"{report.confidence:.2f}" if report else "-",
            inv.mode,
            ((report.summary if report else inv.incident.title) or "")[:70],
        )
    console.print(table)


@handle_errors
def show(
    investigation_id: str = typer.Argument(..., help="Investigation id (inv-...)."),
    events: bool = typer.Option(False, "--events", help="Replay the stored event stream."),
    as_json: bool = typer.Option(False, "--json"),
    env: str | None = EnvOption,
) -> None:
    """Show a stored investigation (and optionally replay its events)."""
    from aiops.cli.investigate_cmd import print_event, print_investigation

    store = open_store(load_settings(env))

    async def load() -> tuple[Investigation | None, list[Any]]:
        inv = await store.get(investigation_id)
        stream = await store.events(investigation_id) if events else []
        return inv, stream

    inv, stream = run(load(), store)
    if inv is None:
        err_console.print(f"[red]No investigation '{investigation_id}' in the store.[/red]")
        raise typer.Exit(code=1)
    if as_json:
        console.print_json(json.dumps(inv.model_dump(mode="json")))
        return
    for event in stream:
        print_event(event)
    print_investigation(inv)
