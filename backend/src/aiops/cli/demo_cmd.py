"""`aiops demo seed|export`: demo investigations from real replays (zero tokens)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import typer

from aiops.cli.common import EnvOption, console, handle_errors
from aiops.cli.history_cmd import open_store
from aiops.core.config import load_settings
from aiops.orchestrator.demo import build_demo, export_demo, seed_demo

app = typer.Typer(
    help="Demo data: S0-S5 replays + a synthetic 14-day history.", no_args_is_help=True
)

NowOption = typer.Option(
    None, "--now", help="Anchor time (UTC). Default: now, rounded down to the hour."
)


def _now(value: datetime | None) -> datetime:
    if value is not None:
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


@app.command("seed")
@handle_errors
def seed(now: datetime | None = NowOption, env: str | None = EnvOption) -> None:
    """Store the demo investigations (mode 'demo'; replaces earlier demo data)."""
    settings = load_settings(env)
    store = open_store(settings)
    anchor = _now(now)

    async def run() -> int:
        try:
            return await seed_demo(store, await build_demo(settings, anchor))
        finally:
            await store.close()

    count = asyncio.run(run())
    console.print(f"[green]Seeded {count} demo investigations[/green] (aiops history --mode demo)")


@app.command("export")
@handle_errors
def export(
    out: Path = typer.Option(Path("demo/export"), "--out", "-o", help="Output directory."),
    now: datetime | None = NowOption,
    env: str | None = EnvOption,
) -> None:
    """Write contract-shaped JSON (list, full investigations, events, dashboard, ...)."""
    settings = load_settings(env)
    anchor = _now(now)
    data = asyncio.run(build_demo(settings, anchor))
    written = export_demo(settings, data, out, anchor)
    console.print(f"[green]Wrote {len(written)} files[/green] to {out}")
