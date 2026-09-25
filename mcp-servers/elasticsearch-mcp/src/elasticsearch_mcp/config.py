"""Server settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_PATTERNS = (
    "payment-prod-*,order-prod-*,user-prod-*,inventory-prod-*,"
    "payment-staging-*,order-staging-*,user-staging-*,inventory-staging-*,logs-k8s-*"
)


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class ServerSettings:
    es_url: str = "http://localhost:9200"
    es_api_key: str | None = None
    es_username: str | None = None
    es_password: str | None = field(default=None, repr=False)
    allowed_index_patterns: tuple[str, ...] = tuple(DEFAULT_PATTERNS.split(","))
    max_time_range_hours: float = 48.0
    max_results: int = 1000
    query_timeout_s: float = 30.0
    timestamp_field: str = "@timestamp"

    @classmethod
    def from_env(cls) -> ServerSettings:
        patterns = tuple(
            p.strip()
            for p in _env("ALLOWED_INDEX_PATTERNS", DEFAULT_PATTERNS).split(",")
            if p.strip()
        )
        return cls(
            es_url=_env("ES_URL", "http://localhost:9200").rstrip("/"),
            es_api_key=os.environ.get("ES_API_KEY") or None,
            es_username=os.environ.get("ES_USERNAME") or None,
            es_password=os.environ.get("ES_PASSWORD") or None,
            allowed_index_patterns=patterns,
            max_time_range_hours=float(_env("MAX_TIME_RANGE_HOURS", "48")),
            max_results=int(_env("MAX_RESULTS", "1000")),
            query_timeout_s=float(_env("QUERY_TIMEOUT_S", "30")),
            timestamp_field=_env("TIMESTAMP_FIELD", "@timestamp"),
        )
