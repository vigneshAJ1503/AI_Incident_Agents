"""Run the server: `elasticsearch-mcp --transport http --port 8101` (or stdio)."""

from __future__ import annotations

import argparse
import logging

from elasticsearch_mcp.config import ServerSettings
from elasticsearch_mcp.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Elasticsearch MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8101)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = ServerSettings.from_env()
    logging.getLogger(__name__).info(
        "elasticsearch-mcp: es=%s allowed=%s max_range=%sh",
        settings.es_url,
        ",".join(settings.allowed_index_patterns),
        settings.max_time_range_hours,
    )
    server = create_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
