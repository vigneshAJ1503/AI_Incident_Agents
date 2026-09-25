"""Entry point: `sample-app` runs the service named by SERVICE_NAME (or the traffic generator)."""

from __future__ import annotations

import asyncio
import os

import uvicorn

from sample_app.app import create_app, generate_traffic
from sample_app.config import Config
from sample_app.logs import JsonLogger


def main() -> None:
    config = Config.from_env()
    if config.service == "traffic-generator":
        asyncio.run(generate_traffic(config, JsonLogger(config)))
        return
    uvicorn.run(
        create_app(config),
        host="0.0.0.0",  # noqa: S104 - inside a container
        port=int(os.environ.get("PORT", "8080")),
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
