"""Run the server: `mock-tickets-mcp --transport http --port 8109` (or stdio)."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mock_tickets_mcp.config import ServerSettings
from mock_tickets_mcp.server import create_server
from mock_tickets_mcp.store import (
    MemoryTicketStore,
    PostgresTicketStore,
    StoreError,
    TicketStore,
)


def build_store(settings: ServerSettings) -> TicketStore:
    if settings.store == "memory":
        return MemoryTicketStore()
    if settings.store == "postgres":
        return PostgresTicketStore(settings.dsn, settings.schema)
    raise SystemExit(f"TICKETS_STORE must be 'postgres' or 'memory', got '{settings.store}'")


async def create_schema(store: PostgresTicketStore, attempts: int = 30) -> None:
    """Create the schema at start (so `aiops seed tickets` can fill it); wait for Postgres."""
    for attempt in range(1, attempts + 1):
        try:
            await store.setup()
            return
        except StoreError as exc:
            if attempt == attempts:
                raise SystemExit(f"Postgres not ready: {exc}") from exc
            logging.getLogger(__name__).warning("waiting for Postgres (%s)", exc)
            await asyncio.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Jira stand-in (mcp-atlassian tools)")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8109)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = ServerSettings.from_env()
    logging.getLogger(__name__).info(
        "mock-tickets-mcp: store=%s schema=%s projects=%s read_only=%s",
        settings.store,
        settings.schema,
        ",".join(settings.allowed_projects) or "*",
        settings.read_only,
    )
    store = build_store(settings)
    if isinstance(store, PostgresTicketStore):
        asyncio.run(create_schema(store))
    server = create_server(settings, store)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
