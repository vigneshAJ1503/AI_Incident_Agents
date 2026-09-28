"""`aiops demo seed|export`: demo investigations from real replays (zero tokens)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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
def seed(
    now: datetime | None = NowOption,
    if_older_than: float | None = typer.Option(
        None,
        "--if-older-than",
        help="Hours. Skip when the newest demo investigation is younger (idempotent `make demo`).",
    ),
    reset: bool = typer.Option(
        False, "--reset", help="Also delete replay investigations started from the UI."
    ),
    env: str | None = EnvOption,
) -> None:
    """Store the demo investigations (mode 'demo'; replaces earlier demo data)."""
    settings = load_settings(env)
    store = open_store(settings)
    anchor = _now(now)

    async def run() -> int | None:
        try:
            if if_older_than is not None and not reset:
                newest = await store.search(mode="demo", limit=1)
                age = datetime.now(UTC) - newest[0].created_at if newest else None
                if age is not None and age < timedelta(hours=if_older_than):
                    return None
            if reset:
                await store.delete_mode("replay")
            return await seed_demo(store, await build_demo(settings, anchor))
        finally:
            await store.close()

    count = asyncio.run(run())
    if count is None:
        console.print(
            f"Demo data is fresh (newer than {if_older_than:g} h): kept it. "
            "`make demo-reset` re-seeds."
        )
        return
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
