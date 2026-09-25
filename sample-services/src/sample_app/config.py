"""Runtime configuration from environment variables (set by the Kubernetes Deployment).

Fault flags live here too: PR-017 injects incidents by changing them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SERVICES = (
    "payment-service",
    "order-service",
    "user-service",
    "inventory-service",
    "traffic-generator",
)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    service: str
    version: str
    environment: str
    namespace: str
    team: str
    host: str
    # dependencies
    database_url: str | None
    redis_url: str | None
    payment_url: str
    user_url: str
    inventory_url: str
    order_url: str
    # tuning / fault flags
    db_pool_size: int
    db_timeout_ms: int
    db_hold_ms: int  # time a request holds its DB connection (transaction work)
    slow_query_ms: int  # inventory: stock query duration
    upstream_timeout_ms: int
    leak_kb_per_request: int  # order: memory leak (S2)
    # traffic generator
    rps: float

    @classmethod
    def from_env(cls) -> Config:
        service = os.environ.get("SERVICE_NAME", "payment-service")
        if service not in SERVICES:
            raise ValueError(f"SERVICE_NAME must be one of {SERVICES}, got {service!r}")
        return cls(
            service=service,
            version=os.environ.get("APP_VERSION", "v0.0.0"),
            environment=os.environ.get("ENVIRONMENT", "production"),
            namespace=os.environ.get("POD_NAMESPACE", "prod"),
            team=os.environ.get("TEAM", "platform"),
            host=os.environ.get("HOSTNAME", "local"),
            database_url=os.environ.get("DATABASE_URL") or None,
            redis_url=os.environ.get("REDIS_URL") or None,
            payment_url=os.environ.get("PAYMENT_URL", "http://payment-service:8080"),
            user_url=os.environ.get("USER_URL", "http://user-service:8080"),
            inventory_url=os.environ.get("INVENTORY_URL", "http://inventory-service:8080"),
            order_url=os.environ.get("ORDER_URL", "http://order-service:8080"),
            db_pool_size=_int("DB_POOL_SIZE", 20),
            db_timeout_ms=_int("DB_TIMEOUT_MS", 5000),
            db_hold_ms=_int("DB_HOLD_MS", 0),
            slow_query_ms=_int("SLOW_QUERY_MS", 20),
            upstream_timeout_ms=_int("UPSTREAM_TIMEOUT_MS", 3000),
            leak_kb_per_request=_int("LEAK_KB_PER_REQUEST", 0),
            rps=float(os.environ.get("TRAFFIC_RPS", "6")),
        )
