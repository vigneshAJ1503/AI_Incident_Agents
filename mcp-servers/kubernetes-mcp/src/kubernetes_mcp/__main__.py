"""Run the server: `kubernetes-mcp --transport http --port 8106` (or stdio)."""

from __future__ import annotations

import argparse
import logging

from kubernetes_mcp.config import ServerSettings
from kubernetes_mcp.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Kubernetes MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8106)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # one line per API call otherwise

    settings = ServerSettings.from_env()
    logging.getLogger(__name__).info(
        "kubernetes-mcp: kubeconfig=%s namespaces=%s max_results=%s max_log_lines=%s",
        settings.kubeconfig or "in-cluster",
        ",".join(settings.allowed_namespaces),
        settings.max_results,
        settings.max_log_lines,
    )
    server = create_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
