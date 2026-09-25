"""`aiops knowledge ...` commands: index the markdown knowledge base into Postgres FTS."""

from __future__ import annotations

from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.table import Table

from aiops.cli.common import console, err_console
from aiops.core.config import ConfigError, find_config_dir
from aiops.knowledge.ingest import DEFAULT_SCHEMA, IngestError, KnowledgeStore

app = typer.Typer(help="Manage the runbook knowledge base.", no_args_is_help=True)


def _repo_root() -> Path | None:
    try:
        return find_config_dir().parent
    except ConfigError:
        return None


@app.command("ingest")
def ingest(
    path: Path | None = typer.Option(
        None, "--path", "-p", help="Knowledge base directory (default: <repo>/knowledge-base)."
    ),
    prefix: str | None = typer.Option(
        None,
        help="Stored path prefix (default: the directory name, e.g. 'knowledge-base'). "
        "Stored paths are repo-relative so doc links resolve.",
    ),
    dsn: str | None = typer.Option(
        None,
        "--dsn",
        help="Postgres connection string (default: $KNOWLEDGE_DATABASE_URL or POSTGRES_*).",
    ),
    schema: str = typer.Option(DEFAULT_SCHEMA, help="Postgres schema to write."),
) -> None:
    """Chunk markdown by headings and upsert into Postgres full-text search (idempotent)."""
    root = _repo_root()
    if root is not None:
        load_dotenv(root / ".env", override=False)
    kb = path or ((root / "knowledge-base") if root else Path("knowledge-base"))
    try:
        report = KnowledgeStore(dsn, schema=schema).ingest(kb.resolve(), prefix)
    except IngestError as exc:
        err_console.print(f"[red]Ingestion failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    plan = report.plan
    table = Table("Change", "Documents")
    for name, paths in [
        ("added", plan.add),
        ("updated", plan.update),
        ("unchanged", plan.unchanged),
        ("deleted", plan.delete),
    ]:
        table.add_row(name, "\n".join(paths) if name != "unchanged" else str(len(paths)))
    console.print(table)
    console.print(
        f"{report.chunks_written} chunks written · index now holds {report.total_docs} documents, "
        f"{report.total_chunks} chunks (schema '{schema}')"
    )
