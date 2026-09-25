"""Run the server: `git-mcp --transport http --port 8107` (or stdio)."""

from __future__ import annotations

import argparse
import logging

from git_mcp.config import ServerSettings
from git_mcp.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Git MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8107)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = ServerSettings.from_env()
    logging.getLogger(__name__).info(
        "git-mcp: repos=%s max_commits=%s max_diff_chars=%s",
        ",".join(f"{k}={v}" for k, v in settings.repos.items()),
        settings.max_commits,
        settings.max_diff_chars,
    )
    server = create_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
