from __future__ import annotations

import asyncio
import io
import json
import sys
from typing import Any

import httpx
import pytest

from sample_app.app import Runtime, create_app
from sample_app.config import Config
from sample_app.deps import Cache, Database
from sample_app.logs import JsonLogger
from sample_app.metrics import Metrics


class FakeConn:
    async def execute(self, query: str, *args: Any) -> str:
        if "pg_sleep" in query:
            await asyncio.sleep(float(args[0]))
        return "OK"

    async def fetchval(self, query: str, *args: Any) -> Any:
        return 42


class FakePool:
    """Real concurrency limit (like asyncpg): at most `size` connections at a time."""

    def __init__(self, size: int) -> None:
        self._sem = asyncio.Semaphore(size)

    async def acquire(self) -> FakeConn:
        await self._sem.acquire()
        return FakeConn()

    async def release(self, conn: FakeConn) -> None:
        self._sem.release()


class BrokenRedis:
    async def get(self, key: str) -> Any:
        raise ConnectionRefusedError("Connection refused")

    async def set(self, key: str, value: str, ex: int) -> Any:
        raise ConnectionRefusedError("Connection refused")


def make(
    service: str, *, upstream: httpx.MockTransport | None = None, redis: Any = None, **env: Any
) -> tuple[httpx.AsyncClient, Runtime, io.StringIO]:
    base: dict[str, Any] = dict(
        service=service,
        version="v1.8.2",
        environment="production",
        namespace="prod",
        team="payments",
        host="pod-1",
        database_url="postgres://fake",
        redis_url="redis://fake" if redis else None,
        payment_url="http://payment-service",
        user_url="http://user-service",
        inventory_url="http://inventory-service",
        order_url="http://order-service",
        db_pool_size=2,
        db_timeout_ms=200,
        db_hold_ms=0,
        slow_query_ms=0,
        upstream_timeout_ms=100,
        leak_kb_per_request=0,
        rps=1.0,
    )
    base.update(env)
    config = Config(**base)
    stream = io.StringIO()
    rt = Runtime(config=config, log=JsonLogger(config, stream), metrics=Metrics())
    rt.db = Database(FakePool(config.db_pool_size), config.db_pool_size, config.db_timeout_ms)
    rt.cache = Cache(redis)
    rt.http = httpx.AsyncClient(
        transport=upstream or httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    app = create_app(config, rt)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, rt, stream


def records(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


async def test_request_log_matches_synthetic_schema() -> None:
    client, _, stream = make("payment-service")
    response = await client.post("/api/v1/pay", json={"order_id": 1, "amount": 5, "user_id": 3})
    assert response.status_code == 200
    [line] = records(stream)
    assert line["message"].startswith("POST /api/v1/pay completed with 200 in ")
    for key in (
        "@timestamp",
        "level",
        "service",
        "environment",
        "version",
        "host",
        "logger",
        "trace_id",
        "http_method",
        "endpoint",
        "status_code",
        "duration_ms",
    ):
        assert key in line
    assert line["endpoint"] == "/api/v1/pay" and line["version"] == "v1.8.2"
    assert response.headers["x-trace-id"] == line["trace_id"]


async def test_pool_exhaustion_logs_s1_signature() -> None:
    client, _, stream = make("payment-service", db_pool_size=2, db_hold_ms=500, db_timeout_ms=100)
    results = await asyncio.gather(*(client.post("/api/v1/pay", json={}) for _ in range(4)))
    assert sorted(r.status_code for r in results) == [200, 200, 500, 500]
    errors = [r for r in records(stream) if r["level"] == "ERROR"]
    assert len(errors) == 2
    assert errors[0]["message"].startswith(
        "Database connection timeout: could not acquire a connection from the pool within 100ms (pool size=2"
    )
    assert errors[0]["error_type"] == "ConnectionTimeoutException"
    assert errors[0]["status_code"] == 500
    # no duplicate "completed" line for failed requests
    assert not any("completed with 500" in r["message"] for r in records(stream))


async def test_redis_outage_falls_back_and_reports() -> None:
    client, _, stream = make("user-service", redis=BrokenRedis())
    response = await client.get("/api/v1/users/7")
    assert response.status_code == 200
    messages = [(r["level"], r["message"]) for r in records(stream)]
    assert ("ERROR", "Redis connection refused: redis:6379 (ECONNREFUSED)") in messages
    assert ("WARN", "Cache unavailable, fell back to database") in messages
    metrics = (await client.get("/metrics")).text
    assert 'redis_up{namespace="prod",service="user-service",team="payments"} 0.0' in metrics


async def test_upstream_timeout_names_the_dependency() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if "inventory" in str(request.url):
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={})

    client, _, stream = make("order-service", upstream=httpx.MockTransport(upstream))
    response = await client.post("/api/v1/orders", json={"sku": 4411})
    assert response.status_code == 504
    [error] = [r for r in records(stream) if r["level"] == "ERROR"]
    assert (
        error["message"] == "Timeout calling inventory-service GET /api/v1/stock/4411 after 100ms"
    )


async def test_payment_failure_seen_by_order_service() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500 if "payment" in str(request.url) else 200, json={})

    client, _, stream = make("order-service", upstream=httpx.MockTransport(upstream))
    response = await client.post("/api/v1/orders", json={})
    assert response.status_code == 502
    [error] = [r for r in records(stream) if r["level"] == "ERROR"]
    assert error["message"] == "Payment call failed: HTTP 500 from payment-service POST /api/v1/pay"


async def test_slow_query_warning() -> None:
    client, _, stream = make("inventory-service", slow_query_ms=1050, db_timeout_ms=5000)
    response = await client.get("/api/v1/stock/1234")
    assert response.status_code == 200
    assert any(
        r["level"] == "WARN" and r["message"].startswith("Slow query on stock_levels took ")
        for r in records(stream)
    )


async def test_memory_leak_flag_grows_memory() -> None:
    client, rt, _ = make("order-service", leak_kb_per_request=64)
    for _ in range(3):
        await client.post("/api/v1/orders", json={})
    assert sum(len(b) for b in rt.leak) == 3 * 64 * 1024


async def test_metrics_contract() -> None:
    client, _, _ = make("payment-service")
    await client.post("/api/v1/pay", json={})
    text = (await client.get("/metrics")).text
    assert (
        'http_requests_total{endpoint="/api/v1/pay",method="POST",namespace="prod",'
        'service="payment-service",status="200",team="payments"} 1.0'
    ) in text
    assert "http_request_duration_seconds_bucket" in text
    assert "db_pool_connections_active" in text and "db_pool_connections_pending" in text
    if sys.platform.startswith("linux"):  # process_* metrics come from /proc
        assert "process_resident_memory_bytes" in text


def test_config_rejects_unknown_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_NAME", "billing")
    with pytest.raises(ValueError, match="SERVICE_NAME"):
        Config.from_env()
