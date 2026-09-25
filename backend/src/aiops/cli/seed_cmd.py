"""`aiops seed ...` commands: load deterministic scenario data into local infra."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import typer
from rich.table import Table

from aiops.cli.common import console, err_console
from aiops.seed.elasticsearch import ElasticsearchSeeder, SeedError
from aiops.seed.logs import ENV_SHORT, SCENARIOS, LogGenerator, SeedWindow, index_name

app = typer.Typer(help="Seed local infrastructure with scenario data.", no_args_is_help=True)


@app.command("logs")
def seed_logs(
    scenario: str = typer.Option("S1", "--scenario", "-S", help=f"One of {', '.join(SCENARIOS)}."),
    hours: float = typer.Option(
        26.0, help="History length (>=24.5h so a 24h-earlier baseline exists)."
    ),
    now: datetime | None = typer.Option(None, help="Anchor time (UTC ISO). Default: now."),
    seed: int = typer.Option(42, help="Random seed; same inputs -> same documents."),
    environment: str = typer.Option("production", "--environment", "-E"),
    es_url: str = typer.Option(
        os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200"),
        "--es-url",
        help="Elasticsearch URL.",
    ),
    keep: bool = typer.Option(False, help="Keep existing seeded indices (default: replace)."),
) -> None:
    """Replace seeded log indices with a scenario's data (background history + incident)."""
    scenario = scenario.upper()
    if scenario not in SCENARIOS:
        raise typer.BadParameter(f"scenario must be one of {SCENARIOS}")
    if environment not in ENV_SHORT:
        raise typer.BadParameter(f"environment must be one of {sorted(ENV_SHORT)}")
    window = SeedWindow.build(now or datetime.now(UTC), hours)
    generator = LogGenerator(scenario, window, seed=seed, environment=environment)
    seeder = ElasticsearchSeeder(es_url)
    try:
        version = seeder.ping()
        seeder.ensure_template()
        if not keep:
            seeder.delete_seeded_indices(ENV_SHORT[environment])
        docs = (
            (index_name(d["service"], environment, datetime.fromisoformat(d["@timestamp"])), d)
            for d in generator.generate()
        )
        counts = seeder.bulk(docs)
        seeder.write_meta(scenario, window, seed, counts)
    except SeedError as exc:
        err_console.print(f"[red]Seeding failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        seeder.close()

    table = Table(title=f"Seeded scenario {scenario} into Elasticsearch {version}")
    table.add_column("Index")
    table.add_column("Documents", justify="right")
    for name, count in sorted(counts.items()):
        table.add_row(name, str(count))
    console.print(table)
    console.print(
        f"window: {window.start:%Y-%m-%d %H:%M} → {window.now:%Y-%m-%d %H:%M} UTC · "
        f"incident starts {window.incident_start:%H:%M} UTC · total {sum(counts.values())} docs"
    )
