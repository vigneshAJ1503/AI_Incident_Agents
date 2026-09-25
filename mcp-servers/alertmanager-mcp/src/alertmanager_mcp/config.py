"""Server settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class ServerSettings:
    am_url: str = "http://localhost:9093"
    am_bearer_token: str | None = field(default=None, repr=False)
    am_username: str | None = None
    am_password: str | None = field(default=None, repr=False)
    max_results: int = 200  # alerts / silences / groups returned per call
    max_filters: int = 10  # label matchers per call
    max_time_range_hours: float = 168.0  # alert_history window
    query_timeout_s: float = 15.0

    @classmethod
    def from_env(cls) -> ServerSettings:
        return cls(
            am_url=_env("AM_URL", "http://localhost:9093").rstrip("/"),
            am_bearer_token=os.environ.get("AM_BEARER_TOKEN") or None,
            am_username=os.environ.get("AM_USERNAME") or None,
            am_password=os.environ.get("AM_PASSWORD") or None,
            max_results=int(_env("MAX_RESULTS", "200")),
            max_filters=int(_env("MAX_FILTERS", "10")),
            max_time_range_hours=float(_env("MAX_TIME_RANGE_HOURS", "168")),
            query_timeout_s=float(_env("QUERY_TIMEOUT_S", "15")),
        )
