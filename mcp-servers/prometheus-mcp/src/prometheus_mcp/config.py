"""Server settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _list(name: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in os.environ.get(name, "").split(",") if p.strip())


@dataclass(frozen=True)
class ServerSettings:
    prom_url: str = "http://localhost:9090"
    prom_bearer_token: str | None = field(default=None, repr=False)
    prom_username: str | None = None
    prom_password: str | None = field(default=None, repr=False)
    query_timeout_s: float = 20.0  # sent to Prometheus (timeout=) and used for HTTP
    max_range_hours: float = 24.0  # query_range end - start, and any [range] in a query
    max_points: int = 1_100  # points per series in query_range (Prometheus' own cap: 11000)
    min_step_s: int = 15  # never finer than this (the scrape interval is 30 s)
    max_series: int = 50  # series returned per query (the rest is reported as truncated)
    max_query_length: int = 2_000
    max_results: int = 500  # list_metrics / get_targets items
    #: Optional allowlist of metric-name regexes (full match). Empty = every metric.
    metric_allowlist: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> ServerSettings:
        return cls(
            prom_url=_env("PROM_URL", "http://localhost:9090").rstrip("/"),
            prom_bearer_token=os.environ.get("PROM_BEARER_TOKEN") or None,
            prom_username=os.environ.get("PROM_USERNAME") or None,
            prom_password=os.environ.get("PROM_PASSWORD") or None,
            query_timeout_s=float(_env("QUERY_TIMEOUT_S", "20")),
            max_range_hours=float(_env("MAX_RANGE_HOURS", "24")),
            max_points=int(_env("MAX_POINTS", "1100")),
            min_step_s=int(_env("MIN_STEP_S", "15")),
            max_series=int(_env("MAX_SERIES", "50")),
            max_query_length=int(_env("MAX_QUERY_LENGTH", "2000")),
            max_results=int(_env("MAX_RESULTS", "500")),
            metric_allowlist=_list("METRIC_ALLOWLIST"),
        )
