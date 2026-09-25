"""Prometheus metrics — the contract used by deploy/compose/config/prometheus/alert-rules.yml."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

LABELS = ("service", "team", "namespace")


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.requests = Counter(
            "http_requests_total",
            "HTTP requests",
            (*LABELS, "method", "endpoint", "status"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration",
            (*LABELS, "method", "endpoint"),
            buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=self.registry,
        )
        self.pool_active = Gauge(
            "db_pool_connections_active", "DB connections in use", LABELS, registry=self.registry
        )
        self.pool_max = Gauge(
            "db_pool_connections_max", "DB pool size", LABELS, registry=self.registry
        )
        self.pool_pending = Gauge(
            "db_pool_connections_pending",
            "Requests waiting for a DB connection",
            LABELS,
            registry=self.registry,
        )
        self.redis_up = Gauge(
            "redis_up", "1 if the last Redis operation succeeded", LABELS, registry=self.registry
        )
