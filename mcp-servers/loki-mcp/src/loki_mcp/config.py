"""Server settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def parse_streams(raw: str) -> tuple[tuple[str, str], ...]:
    """``namespace=prod,namespace=staging`` -> (("namespace", "prod"), ("namespace", "staging"))."""
    pairs: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        label, sep, value = part.partition("=")
        if not sep or not label.strip() or not value.strip():
            raise ValueError(f"ALLOWED_STREAMS entry '{part}' must be label=value")
        pairs.append((label.strip(), value.strip().strip('"')))
    return tuple(pairs)


@dataclass(frozen=True)
class ServerSettings:
    loki_url: str = "http://localhost:3100"
    loki_bearer_token: str | None = field(default=None, repr=False)
    loki_username: str | None = None
    loki_password: str | None = field(default=None, repr=False)
    #: Tenant (multi-tenant Loki / Grafana Cloud): sent as X-Scope-OrgID.
    loki_org_id: str | None = None
    #: Every stream selector must contain an exact ``label="value"`` matcher from this list
    #: (e.g. namespace=prod). Empty = any selector (not recommended outside tests).
    allowed_streams: tuple[tuple[str, str], ...] = (("namespace", "prod"),)
    query_timeout_s: float = 30.0
    max_range_hours: float = 48.0  # end - start, every [range] and every offset
    max_lines: int = 500  # log lines per query_range (Loki's own cap: max_entries_limit_per_query)
    max_series: int = 1_000  # series per metric result
    max_points: int = 1_100  # points per series in a metric query_range
    min_step_s: int = 1
    max_query_length: int = 4_000
    max_results: int = 500  # label names/values per listing

    @classmethod
    def from_env(cls) -> ServerSettings:
        return cls(
            loki_url=_env("LOKI_URL", "http://localhost:3100").rstrip("/"),
            loki_bearer_token=os.environ.get("LOKI_BEARER_TOKEN") or None,
            loki_username=os.environ.get("LOKI_USERNAME") or None,
            loki_password=os.environ.get("LOKI_PASSWORD") or None,
            loki_org_id=os.environ.get("LOKI_ORG_ID") or None,
            allowed_streams=parse_streams(os.environ.get("ALLOWED_STREAMS", "namespace=prod")),
            query_timeout_s=float(_env("QUERY_TIMEOUT_S", "30")),
            max_range_hours=float(_env("MAX_RANGE_HOURS", "48")),
            max_lines=int(_env("MAX_LINES", "500")),
            max_series=int(_env("MAX_SERIES", "1000")),
            max_points=int(_env("MAX_POINTS", "1100")),
            min_step_s=int(_env("MIN_STEP_S", "1")),
            max_query_length=int(_env("MAX_QUERY_LENGTH", "4000")),
            max_results=int(_env("MAX_RESULTS", "500")),
        )
