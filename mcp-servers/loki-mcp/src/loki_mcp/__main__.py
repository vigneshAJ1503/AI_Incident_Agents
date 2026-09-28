"""Run the server: `loki-mcp --transport http --port 8110` (or stdio)."""

from __future__ import annotations

import argparse
import logging

from loki_mcp.config import ServerSettings
from loki_mcp.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Loki MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8110)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = ServerSettings.from_env()
    logging.getLogger(__name__).info(
        "loki-mcp: loki=%s allowed_streams=%s max_range=%sh max_lines=%s max_series=%s",
        settings.loki_url,
        ",".join(f"{k}={v}" for k, v in settings.allowed_streams) or "(any)",
        settings.max_range_hours,
        settings.max_lines,
        settings.max_series,
    )
    server = create_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
