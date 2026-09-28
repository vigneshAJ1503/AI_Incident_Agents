"""`aiops serve`: the REST + SSE API for the Web UI (PR-035, docs/api/README.md)."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

import typer

from aiops.cli.common import EnvOption, console, handle_errors
from aiops.core.config import PROFILE_VAR, load_settings


class JsonFormatter(logging.Formatter):
    """One JSON object per line (access-log records are JSON messages already)."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        try:
            payload = json.loads(message) if message.startswith("{") else {"message": message}
        except ValueError:
            payload = {"message": message}
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            **payload,
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def log_config(level: str) -> dict[str, object]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": JsonFormatter}},
        "handlers": {
            "stdout": {"class": "logging.StreamHandler", "formatter": "json", "stream": sys.stdout}
        },
        "root": {"handlers": ["stdout"], "level": level.upper()},
        "loggers": {
            "uvicorn.access": {"handlers": [], "propagate": False},  # we log our own
        },
    }


@handle_errors
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (keep 127.0.0.1 locally)."),
    port: int = typer.Option(8000, "--port", help="Port."),
    reload: bool = typer.Option(False, "--reload", help="Restart on code changes (development)."),
    log_level: str = typer.Option("info", "--log-level", help="debug | info | warning | error"),
    env: str | None = EnvOption,
) -> None:
    """Serve the API (OpenAPI at /api/docs). The profile comes from $AIOPS_PROFILE."""
    import uvicorn

    if env:
        os.environ[PROFILE_VAR] = env  # also for --reload's worker processes
    settings = load_settings(env)  # fail fast with a readable error
    console.print(
        f"[bold]aiops API[/bold] profile [cyan]{settings.profile}[/cyan] on "
        f"http://{host}:{port}/api  (docs: http://{host}:{port}/api/docs)"
    )
    uvicorn.run(
        "aiops.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_config=log_config(log_level),
        access_log=False,
        proxy_headers=False,
        timeout_graceful_shutdown=10,
    )
