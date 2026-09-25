"""Deterministic synthetic application logs for scenarios S0-S5 (PR-007).

Generates ~26h of realistic JSON logs for four services, with background noise
(recurring errors at a normal rate) and, for S1-S5, an incident in the last ~20
minutes. The same (scenario, seed, now) always produces the same documents.

Replaced by real Kubernetes logs in PR-016 — the Log agent must work on both
with configuration changes only.
"""

from __future__ import annotations

import random
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

SCENARIOS = ("S0", "S1", "S2", "S3", "S4", "S5")
ENV_SHORT = {"production": "prod", "staging": "staging"}

INCIDENT_OFFSET = timedelta(minutes=20)  # incident starts at now - 20m
DEPLOY_OFFSET = timedelta(minutes=22)  # S1 rollout at now - 22m


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    weight: float


@dataclass(frozen=True)
class ServiceProfile:
    name: str
    short: str
    version: str
    rate_per_min: float
    latency_ms: tuple[float, float]  # mean, stddev
    endpoints: tuple[Endpoint, ...]
    noise: tuple[str, int, str, str]  # message template, status, level, error_type
    noise_rate: float
    logger: str
    pods: tuple[str, ...] = field(default=())


SERVICES: dict[str, ServiceProfile] = {
    p.name: p
    for p in (
        ServiceProfile(
            name="payment-service",
            short="payment",
            version="v1.8.1",
            rate_per_min=12,
            latency_ms=(45, 15),
            endpoints=(
                Endpoint("POST", "/api/v1/pay", 0.7),
                Endpoint("GET", "/api/v1/payments/{id}", 0.3),
            ),
            noise=(
                "Payment declined by issuer: insufficient funds (order {id})",
                402,
                "ERROR",
                "PaymentDeclinedException",
            ),
            noise_rate=0.004,
            logger="com.acme.payment.PaymentController",
        ),
        ServiceProfile(
            name="order-service",
            short="order",
            version="v2.3.0",
            rate_per_min=10,
            latency_ms=(60, 20),
            endpoints=(
                Endpoint("POST", "/api/v1/orders", 0.6),
                Endpoint("GET", "/api/v1/orders/{id}", 0.4),
            ),
            noise=(
                "Validation failed: quantity must be positive",
                400,
                "WARN",
                "ValidationException",
            ),
            noise_rate=0.006,
            logger="com.acme.order.OrderController",
        ),
        ServiceProfile(
            name="user-service",
            short="user",
            version="v3.1.4",
            rate_per_min=10,
            latency_ms=(25, 8),
            endpoints=(
                Endpoint("POST", "/api/v1/login", 0.5),
                Endpoint("GET", "/api/v1/users/{id}", 0.5),
            ),
            noise=("Invalid credentials for user {id}", 401, "WARN", "AuthenticationException"),
            noise_rate=0.01,
            logger="com.acme.user.AuthController",
        ),
        ServiceProfile(
            name="inventory-service",
            short="inventory",
            version="v1.4.2",
            rate_per_min=8,
            latency_ms=(30, 10),
            endpoints=(
                Endpoint("GET", "/api/v1/stock/{sku}", 0.7),
                Endpoint("POST", "/api/v1/reservations", 0.3),
            ),
            noise=("SKU {id} not found", 404, "WARN", "NotFoundException"),
            noise_rate=0.005,
            logger="com.acme.inventory.StockController",
        ),
    )
}


def index_name(service: str, environment: str, ts: datetime) -> str:
    return f"{SERVICES[service].short}-{ENV_SHORT[environment]}-{ts:%Y.%m.%d}"


def index_pattern(service: str, environment: str) -> str:
    return f"{SERVICES[service].short}-{ENV_SHORT[environment]}-*"


@dataclass(frozen=True)
class SeedWindow:
    now: datetime
    start: datetime
    incident_start: datetime
    deploy_time: datetime

    @classmethod
    def build(cls, now: datetime, hours: float) -> SeedWindow:
        now = now.astimezone(UTC).replace(second=0, microsecond=0)
        return cls(
            now=now,
            start=now - timedelta(hours=hours),
            incident_start=now - INCIDENT_OFFSET,
            deploy_time=now - DEPLOY_OFFSET,
        )


class LogGenerator:
    def __init__(
        self,
        scenario: str,
        window: SeedWindow,
        *,
        seed: int = 42,
        environment: str = "production",
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"Unknown scenario '{scenario}' (known: {', '.join(SCENARIOS)})")
        self.scenario = scenario
        self.window = window
        self.environment = environment
        self.rng = random.Random(f"{seed}:{scenario}")  # noqa: S311 - test data, not crypto

    # -- helpers -----------------------------------------------------------------------

    def _in_incident(self, ts: datetime) -> bool:
        return self.scenario != "S0" and ts >= self.window.incident_start

    def _version(self, svc: ServiceProfile, ts: datetime) -> str:
        if (
            self.scenario == "S1"
            and svc.name == "payment-service"
            and ts >= self.window.deploy_time
        ):
            return "v1.8.2"
        return svc.version

    def _pod(self, svc: ServiceProfile, version: str) -> str:
        rs = format(zlib.crc32(f"{svc.name}:{version}".encode()) % 0xFFFFF, "05x")
        return f"{svc.name}-{rs}-{'abc'[self.rng.randrange(3)]}{self.rng.randrange(10)}x"

    def _doc(
        self,
        svc: ServiceProfile,
        ts: datetime,
        level: str,
        message: str,
        *,
        status: int | None = None,
        endpoint: Endpoint | None = None,
        duration_ms: int | None = None,
        error_type: str | None = None,
        trace_id: str | None = None,
        logger: str | None = None,
    ) -> dict[str, Any]:
        version = self._version(svc, ts)
        doc: dict[str, Any] = {
            "@timestamp": ts.isoformat().replace("+00:00", "Z"),
            "level": level,
            "message": message,
            "service": svc.name,
            "environment": self.environment,
            "version": version,
            "host": self._pod(svc, version),
            "logger": logger or svc.logger,
            "trace_id": trace_id or format(self.rng.getrandbits(64), "016x"),
        }
        if endpoint is not None:
            doc["http_method"] = endpoint.method
            doc["endpoint"] = endpoint.path
        if status is not None:
            doc["status_code"] = status
        if duration_ms is not None:
            doc["duration_ms"] = duration_ms
        if error_type is not None:
            doc["error_type"] = error_type
        return doc

    def _latency(self, svc: ServiceProfile, factor: float = 1.0) -> int:
        mean, std = svc.latency_ms
        return max(1, int(self.rng.gauss(mean, std) * factor))

    # -- generation ----------------------------------------------------------------------

    def generate(self) -> Iterator[dict[str, Any]]:
        yield from self._lifecycle_events()
        for svc in SERVICES.values():
            ts = self.window.start
            rate_per_s = svc.rate_per_min / 60
            while True:
                ts += timedelta(seconds=self.rng.expovariate(rate_per_s))
                if ts >= self.window.now:
                    break
                yield from self._request(svc, ts)

    def _lifecycle_events(self) -> Iterator[dict[str, Any]]:
        w = self.window
        payment = SERVICES["payment-service"]
        for i in range(3):
            yield self._doc(
                payment,
                w.start + timedelta(minutes=5, seconds=i * 20),
                "INFO",
                f"Starting payment-service {payment.version} (DB_POOL_SIZE=20, DB_TIMEOUT_MS=5000)",
                logger="com.acme.payment.Application",
            )
        if self.scenario == "S1":
            for i in range(3):
                yield self._doc(
                    payment,
                    w.deploy_time + timedelta(seconds=i * 20),
                    "INFO",
                    "Starting payment-service v1.8.2 (DB_POOL_SIZE=2, DB_TIMEOUT_MS=5000)",
                    logger="com.acme.payment.Application",
                )
        if self.scenario == "S2":
            order = SERVICES["order-service"]
            restart = w.incident_start + timedelta(minutes=3)
            count = 1
            while restart < w.now:
                yield self._doc(
                    order,
                    restart,
                    "INFO",
                    f"Starting order-service {order.version} (restart count={count})",
                    logger="com.acme.order.Application",
                )
                restart += timedelta(minutes=4)
                count += 1

    def _request(self, svc: ServiceProfile, ts: datetime) -> Iterator[dict[str, Any]]:
        rng = self.rng
        endpoint = rng.choices(svc.endpoints, weights=[e.weight for e in svc.endpoints])[0]
        trace_id = format(rng.getrandbits(64), "016x")
        incident = self._in_incident(ts)

        special = self._scenario_request(svc, ts, endpoint, trace_id) if incident else None
        if special is not None:
            yield from special
            return

        if rng.random() < svc.noise_rate:
            template, status, level, error_type = svc.noise
            yield self._doc(
                svc,
                ts,
                level,
                template.format(id=rng.randrange(10_000, 99_999)),
                status=status,
                endpoint=endpoint,
                duration_ms=self._latency(svc),
                error_type=error_type,
                trace_id=trace_id,
            )
            return

        factor = self._latency_factor(svc) if incident else 1.0
        duration = self._latency(svc, factor)
        yield self._doc(
            svc,
            ts,
            "INFO",
            f"{endpoint.method} {endpoint.path} completed with 200 in {duration}ms",
            status=200,
            endpoint=endpoint,
            duration_ms=duration,
            trace_id=trace_id,
        )

    def _latency_factor(self, svc: ServiceProfile) -> float:
        return {
            ("S1", "payment-service"): 6.0,
            ("S3", "inventory-service"): 40.0,
            ("S3", "order-service"): 8.0,
            ("S5", "payment-service"): 4.0,
            ("S5", "order-service"): 4.0,
            ("S5", "user-service"): 4.0,
            ("S5", "inventory-service"): 4.0,
        }.get((self.scenario, svc.name), 1.0)

    def _scenario_request(
        self, svc: ServiceProfile, ts: datetime, endpoint: Endpoint, trace_id: str
    ) -> list[dict[str, Any]] | None:
        """Incident-specific log lines for one request, or None for normal behaviour."""
        rng, s, name = self.rng, self.scenario, svc.name

        def error(
            message: str, status: int, error_type: str, duration: int, **kw: Any
        ) -> dict[str, Any]:
            return self._doc(
                svc,
                ts,
                "ERROR",
                message,
                status=status,
                endpoint=endpoint,
                duration_ms=duration,
                error_type=error_type,
                trace_id=trace_id,
                **kw,
            )

        if (
            s == "S1"
            and name == "payment-service"
            and endpoint.path == "/api/v1/pay"
            and rng.random() < 0.35
        ):
            waiting = rng.randrange(5, 40)
            docs = [
                error(
                    "Database connection timeout: could not acquire a connection from the pool "
                    f"within 5000ms (pool size=2, active=2, waiting={waiting})",
                    500,
                    "ConnectionTimeoutException",
                    5000 + rng.randrange(0, 400),
                    logger="com.acme.payment.db.ConnectionPool",
                )
            ]
            if rng.random() < 0.3:
                docs.append(
                    self._doc(
                        svc,
                        ts,
                        "WARN",
                        "HikariPool-1 - Connection is not available, request timed out after 5000ms",
                        trace_id=trace_id,
                        logger="com.zaxxer.hikari.pool.HikariPool",
                    )
                )
            return docs
        if (
            s == "S1"
            and name == "order-service"
            and endpoint.method == "POST"
            and rng.random() < 0.2
        ):
            return [
                error(
                    "Payment call failed: HTTP 500 from payment-service POST /api/v1/pay",
                    502,
                    "UpstreamServiceException",
                    5100 + rng.randrange(0, 300),
                )
            ]
        if s == "S2" and name == "order-service":
            roll = rng.random()
            if roll < 0.15:
                return [
                    error(
                        "java.lang.OutOfMemoryError: Java heap space",
                        503,
                        "OutOfMemoryError",
                        self._latency(svc, 3),
                    )
                ]
            if roll < 0.3:
                heap = rng.randrange(88, 99)
                return [
                    self._doc(
                        svc,
                        ts,
                        "WARN",
                        f"GC overhead limit approaching: heap usage {heap}% after full GC",
                        trace_id=trace_id,
                        logger="jvm.gc",
                    )
                ]
        if s == "S3" and name == "inventory-service" and rng.random() < 0.5:
            took = 3000 + rng.randrange(0, 2500)
            return [
                self._doc(
                    svc,
                    ts,
                    "WARN",
                    f"Slow query on stock_levels took {took}ms",
                    status=200,
                    endpoint=endpoint,
                    duration_ms=took,
                    trace_id=trace_id,
                    logger="com.acme.inventory.db.StockRepository",
                )
            ]
        if s == "S3" and name == "order-service" and rng.random() < 0.3:
            return [
                error(
                    f"Timeout calling inventory-service GET /api/v1/stock/{rng.randrange(1000, 9999)} after 3000ms",
                    504,
                    "UpstreamTimeoutException",
                    3000 + rng.randrange(0, 100),
                )
            ]
        if s == "S4" and name == "user-service" and rng.random() < 0.2:
            return [
                self._doc(
                    svc,
                    ts,
                    "WARN",
                    f"Request queue depth high: {rng.randrange(60, 140)} pending (ready replicas 1/3)",
                    trace_id=trace_id,
                    logger="com.acme.user.RequestQueue",
                )
            ]
        if s == "S4" and name in ("order-service", "payment-service") and rng.random() < 0.1:
            return [
                error(
                    "HTTP 503 from user-service: no healthy upstream",
                    503,
                    "UpstreamServiceException",
                    self._latency(svc, 2),
                )
            ]
        if s == "S5" and rng.random() < 0.3:
            docs = [
                self._doc(
                    svc,
                    ts,
                    "ERROR",
                    "Redis connection refused: redis:6379 (ECONNREFUSED)",
                    trace_id=trace_id,
                    error_type="RedisConnectionException",
                    logger="io.lettuce.core.RedisClient",
                )
            ]
            status = 503 if rng.random() < 0.1 else 200
            duration = self._latency(svc, 4)
            docs.append(
                self._doc(
                    svc,
                    ts,
                    "WARN" if status == 200 else "ERROR",
                    f"Cache unavailable, fell back to database ({endpoint.method} {endpoint.path} {status} in {duration}ms)",
                    status=status,
                    endpoint=endpoint,
                    duration_ms=duration,
                    trace_id=trace_id,
                )
            )
            return docs
        return None


INDEX_TEMPLATE: dict[str, Any] = {
    # Explicit per-service patterns (a broad "*-prod-*" collides with built-in templates).
    "index_patterns": [
        f"{s.short}-{env}-*" for s in SERVICES.values() for env in ENV_SHORT.values()
    ],
    "priority": 200,
    "template": {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {
            "dynamic": "false",
            "properties": {
                "@timestamp": {"type": "date"},
                "level": {"type": "keyword"},
                "message": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
                },
                "service": {"type": "keyword"},
                "environment": {"type": "keyword"},
                "version": {"type": "keyword"},
                "host": {"type": "keyword"},
                "logger": {"type": "keyword"},
                "trace_id": {"type": "keyword"},
                "http_method": {"type": "keyword"},
                "endpoint": {"type": "keyword"},
                "status_code": {"type": "integer"},
                "duration_ms": {"type": "integer"},
                "error_type": {"type": "keyword"},
            },
        },
    },
}
