"""Run the server: `knowledge-mcp --transport http --port 8108` (or stdio)."""

from __future__ import annotations

import argparse
import logging

from knowledge_mcp.config import ServerSettings
from knowledge_mcp.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only runbook search MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8108)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = ServerSettings.from_env()
    logging.getLogger(__name__).info(
        "knowledge-mcp: schema=%s ts_config=%s max_k=%s",
        settings.schema,
        settings.ts_config,
        settings.max_k,
    )
    server = create_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
