"""Structured JSON logs with the same fields as the synthetic generator (aiops/seed/logs.py),
so the Log agent works on real and synthetic logs with configuration only.
"""

from __future__ import annotations

import json
import secrets
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from sample_app.config import Config


def new_trace_id() -> str:
    return secrets.token_hex(8)


class JsonLogger:
    def __init__(self, config: Config, stream: TextIO | None = None) -> None:
        self._config = config
        self._stream = stream or sys.stdout

    def log(self, level: str, message: str, *, logger: str | None = None, **fields: Any) -> None:
        record: dict[str, Any] = {
            "@timestamp": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": level,
            "message": message,
            "service": self._config.service,
            "environment": self._config.environment,
            "version": self._config.version,
            "host": self._config.host,
            "logger": logger or f"com.acme.{self._config.service.split('-')[0]}",
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        self._stream.write(json.dumps(record) + "\n")
        self._stream.flush()

    def info(self, message: str, **fields: Any) -> None:
        self.log("INFO", message, **fields)

    def warn(self, message: str, **fields: Any) -> None:
        self.log("WARN", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self.log("ERROR", message, **fields)
