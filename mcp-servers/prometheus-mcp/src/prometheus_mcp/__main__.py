"""Run the server: `prometheus-mcp --transport http --port 8103` (or stdio)."""

from __future__ import annotations

import argparse
import logging

from prometheus_mcp.config import ServerSettings
from prometheus_mcp.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Prometheus MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8103)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = ServerSettings.from_env()
    logging.getLogger(__name__).info(
        "prometheus-mcp: prom=%s max_range=%sh max_points=%s max_series=%s allowlist=%s",
        settings.prom_url,
        settings.max_range_hours,
        settings.max_points,
        settings.max_series,
        ",".join(settings.metric_allowlist) or "(all)",
    )
    server = create_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
