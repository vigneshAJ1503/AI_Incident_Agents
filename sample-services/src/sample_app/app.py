"""One FastAPI codebase, four services (selected by SERVICE_NAME).

Call graph:  order -> inventory (stock), order -> payment (pay) -> user (lookup)
Backends:    payment/order/inventory/user -> Postgres (pool);  payment/user -> Redis (cache)
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from sample_app.config import Config
from sample_app.deps import SCHEMA, Cache, Database, PoolTimeoutError
from sample_app.logs import JsonLogger, new_trace_id
from sample_app.metrics import Metrics

LOGGERS = {
    "db": "com.acme.{short}.db.ConnectionPool",
    "redis": "io.lettuce.core.RedisClient",
    "app": "com.acme.{short}.Application",
    "http": "com.acme.{short}.{kind}Controller",
}


@dataclass
class Runtime:
    config: Config
    log: JsonLogger
    metrics: Metrics
    db: Database | None = None
    cache: Cache = field(default_factory=lambda: Cache(None))
    http: httpx.AsyncClient | None = None
    leak: list[bytearray] = field(default_factory=list)

    def logger(self, kind: str) -> str:
        short = self.config.service.split("-")[0]
        return LOGGERS[kind].format(short=short, kind=short.capitalize())

    @property
    def labels(self) -> dict[str, str]:
        return {
            "service": self.config.service,
            "team": self.config.team,
            "namespace": self.config.namespace,
        }


class HandledError(Exception):
    """An error already logged by the handler; it becomes a plain status response."""

    def __init__(self, status: int) -> None:
        self.status = status


def create_app(config: Config, runtime: Runtime | None = None) -> FastAPI:
    rt = runtime or Runtime(config=config, log=JsonLogger(config), metrics=Metrics())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if runtime is None:  # real startup; tests inject a prepared runtime
            if config.database_url:
                rt.db = await _connect_db(config)
            rt.cache = Cache.from_url(config.redis_url)
            rt.http = httpx.AsyncClient()
        rt.metrics.pool_max.labels(**rt.labels).set(config.db_pool_size)
        rt.log.info(
            f"Starting {config.service} {config.version} "
            f"(DB_POOL_SIZE={config.db_pool_size}, DB_TIMEOUT_MS={config.db_timeout_ms})",
            logger=rt.logger("app"),
        )
        yield
        if runtime is None:
            if rt.db:
                await rt.db.close()
            await rt.cache.close()
            if rt.http:
                await rt.http.aclose()

    app = FastAPI(title=config.service, lifespan=lifespan)
    app.state.rt = rt

    @app.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in ("/health", "/ready", "/metrics"):
            return await call_next(request)
        trace_id = request.headers.get("x-trace-id") or new_trace_id()
        request.state.trace_id = trace_id
        started = time.perf_counter()
        response = await call_next(request)
        status = response.status_code
        route = request.scope.get("route")
        endpoint = getattr(route, "path", request.url.path)
        elapsed = time.perf_counter() - started
        rt.metrics.requests.labels(
            **rt.labels, method=request.method, endpoint=endpoint, status=str(status)
        ).inc()
        rt.metrics.duration.labels(**rt.labels, method=request.method, endpoint=endpoint).observe(
            elapsed
        )
        if rt.db:
            rt.metrics.pool_active.labels(**rt.labels).set(rt.db.active)
            rt.metrics.pool_pending.labels(**rt.labels).set(rt.db.waiting)
        if not getattr(request.state, "logged_error", False):
            rt.log.info(
                f"{request.method} {endpoint} completed with {status} in {int(elapsed * 1000)}ms",
                logger=rt.logger("http"),
                trace_id=trace_id,
                http_method=request.method,
                endpoint=endpoint,
                status_code=status,
                duration_ms=int(elapsed * 1000),
            )
        response.headers["x-trace-id"] = trace_id
        return response

    @app.exception_handler(HandledError)
    async def handled(request: Request, exc: HandledError) -> Response:
        request.state.logged_error = True
        return JSONResponse({"error": exc.status}, status_code=exc.status)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> Response:
        ok = rt.db is not None or not config.database_url
        return JSONResponse({"ready": ok}, status_code=200 if ok else 503)

    @app.get("/metrics")
    async def metrics() -> Response:
        if config.redis_url:
            rt.metrics.redis_up.labels(**rt.labels).set(1 if rt.cache.healthy else 0)
        return Response(generate_latest(rt.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    ROUTES[config.service](app, rt)
    return app


async def _connect_db(config: Config) -> Database:
    dsn = config.database_url or ""
    for attempt in range(30):  # Postgres may still be starting
        try:
            db = await Database.connect(dsn, config.db_pool_size, config.db_timeout_ms)
            async with db.connection() as conn:
                await conn.execute(SCHEMA)
            return db
        except Exception:  # asyncpg raises several types while Postgres starts
            if attempt == 29:
                raise
            await asyncio.sleep(2)
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------- helpers


def _error(
    rt: Runtime, request: Request, message: str, status: int, error_type: str, kind: str = "http"
) -> HandledError:
    endpoint = getattr(request.scope.get("route"), "path", request.url.path)
    rt.log.error(
        message,
        logger=rt.logger(kind),
        trace_id=request.state.trace_id,
        http_method=request.method,
        endpoint=endpoint,
        status_code=status,
        error_type=error_type,
    )
    return HandledError(status)


async def _db_work(rt: Runtime, request: Request, query: str, *args: Any, hold_ms: int = 0) -> Any:
    if rt.db is None:
        raise _error(rt, request, "Database not configured", 503, "DatabaseUnavailable", "db")
    try:
        async with rt.db.connection() as conn:
            if hold_ms:
                await conn.execute("SELECT pg_sleep($1)", hold_ms / 1000)
            return await conn.fetchval(query, *args)
    except PoolTimeoutError as exc:
        raise _error(rt, request, str(exc), 500, "ConnectionTimeoutException", "db") from None


def _report_cache_failure(rt: Runtime, request: Request) -> None:
    if rt.config.redis_url:
        rt.log.error(
            "Redis connection refused: redis:6379 (ECONNREFUSED)",
            logger=rt.logger("redis"),
            trace_id=request.state.trace_id,
            error_type="RedisConnectionException",
        )
        rt.log.warn(
            "Cache unavailable, fell back to database",
            logger=rt.logger("http"),
            trace_id=request.state.trace_id,
        )


async def _cached(rt: Runtime, request: Request, key: str) -> str | None:
    ok, value = await rt.cache.get(key)
    if not ok:
        _report_cache_failure(rt, request)
    return value


async def _call(
    rt: Runtime,
    request: Request,
    method: str,
    url: str,
    target: str,
    path: str,
    timeout_ms: int,
    **kw: Any,
) -> httpx.Response:
    if rt.http is None:
        raise _error(rt, request, "HTTP client not ready", 503, "NotReady")
    headers = {"x-trace-id": request.state.trace_id}
    try:
        response = await rt.http.request(
            method, url + path, headers=headers, timeout=timeout_ms / 1000, **kw
        )
    except httpx.TimeoutException:
        raise _error(
            rt,
            request,
            f"Timeout calling {target} {method} {path} after {timeout_ms}ms",
            504,
            "UpstreamTimeoutException",
        ) from None
    except httpx.HTTPError:
        raise _error(
            rt,
            request,
            f"HTTP 503 from {target}: no healthy upstream",
            503,
            "UpstreamServiceException",
        ) from None
    if response.status_code >= 500:
        message = (
            f"Payment call failed: HTTP {response.status_code} from {target} {method} {path}"
            if target == "payment-service"
            else f"HTTP {response.status_code} from {target}: {method} {path}"
        )
        raise _error(rt, request, message, 502, "UpstreamServiceException")
    return response


# --------------------------------------------------------------------------- services


def payment_routes(app: FastAPI, rt: Runtime) -> None:
    @app.post("/api/v1/pay")
    async def pay(request: Request) -> dict[str, Any]:
        body = await request.json()
        user_id = int(body.get("user_id", 1))
        if await _cached(rt, request, f"user:{user_id}") is None:
            await _call(
                rt,
                request,
                "GET",
                rt.config.user_url,
                "user-service",
                f"/api/v1/users/{user_id}",
                rt.config.upstream_timeout_ms,
            )
            await rt.cache.set(f"user:{user_id}", "1")
        payment_id = await _db_work(
            rt,
            request,
            "INSERT INTO payments(order_id, amount) VALUES ($1, $2) RETURNING id",
            int(body.get("order_id", 0)),
            float(body.get("amount", 10)),
            hold_ms=rt.config.db_hold_ms,
        )
        return {"payment_id": payment_id, "status": "captured"}

    @app.get("/api/v1/payments/{payment_id}")
    async def get_payment(payment_id: int, request: Request) -> dict[str, Any]:
        amount = await _db_work(
            rt, request, "SELECT amount FROM payments WHERE id = $1", payment_id
        )
        return {"payment_id": payment_id, "amount": float(amount) if amount is not None else None}


def order_routes(app: FastAPI, rt: Runtime) -> None:
    @app.post("/api/v1/orders")
    async def create_order(request: Request) -> dict[str, Any]:
        if rt.config.leak_kb_per_request:  # S2: unbounded in-memory cache
            rt.leak.append(bytearray(rt.config.leak_kb_per_request * 1024))
        body = await request.json()
        sku, qty = int(body.get("sku", 1000)), int(body.get("quantity", 1))
        await _call(
            rt,
            request,
            "GET",
            rt.config.inventory_url,
            "inventory-service",
            f"/api/v1/stock/{sku}",
            rt.config.upstream_timeout_ms,
        )
        order_id = await _db_work(
            rt, request, "INSERT INTO orders(sku, quantity) VALUES ($1, $2) RETURNING id", sku, qty
        )
        await _call(
            rt,
            request,
            "POST",
            rt.config.payment_url,
            "payment-service",
            "/api/v1/pay",
            rt.config.db_timeout_ms + 2000,
            json={"order_id": order_id, "amount": 10 * qty, "user_id": body.get("user_id", 1)},
        )
        return {"order_id": order_id, "status": "confirmed"}

    @app.get("/api/v1/orders/{order_id}")
    async def get_order(order_id: int, request: Request) -> dict[str, Any]:
        sku = await _db_work(rt, request, "SELECT sku FROM orders WHERE id = $1", order_id)
        return {"order_id": order_id, "sku": sku}


def inventory_routes(app: FastAPI, rt: Runtime) -> None:
    @app.get("/api/v1/stock/{sku}")
    async def stock(sku: int, request: Request) -> dict[str, Any]:
        started = time.perf_counter()
        qty = await _db_work(
            rt,
            request,
            "SELECT quantity FROM stock_levels WHERE sku = $1",
            sku,
            hold_ms=rt.config.slow_query_ms,
        )
        took = int((time.perf_counter() - started) * 1000)
        if took >= 1000:
            rt.log.warn(
                f"Slow query on stock_levels took {took}ms",
                logger="com.acme.inventory.db.StockRepository",
                trace_id=request.state.trace_id,
                duration_ms=took,
            )
        return {"sku": sku, "quantity": qty}

    @app.post("/api/v1/reservations")
    async def reserve(request: Request) -> dict[str, Any]:
        body = await request.json()
        await _db_work(
            rt,
            request,
            "UPDATE stock_levels SET quantity = quantity - 1 WHERE sku = $1 RETURNING quantity",
            int(body.get("sku", 1000)),
        )
        return {"reserved": True}


def user_routes(app: FastAPI, rt: Runtime) -> None:
    @app.post("/api/v1/login")
    async def login(request: Request) -> dict[str, Any]:
        body = await request.json()
        user_id = int(body.get("user_id", 1))
        if not await rt.cache.set(f"session:{user_id}", new_trace_id(), ttl_s=900):
            _report_cache_failure(rt, request)
        name = await _db_work(rt, request, "SELECT name FROM users WHERE id = $1", user_id)
        return {"user_id": user_id, "name": name, "session": True}

    @app.get("/api/v1/users/{user_id}")
    async def get_user(user_id: int, request: Request) -> dict[str, Any]:
        cached = await _cached(rt, request, f"profile:{user_id}")
        if cached is not None:
            return {"user_id": user_id, "name": cached}
        name = await _db_work(rt, request, "SELECT name FROM users WHERE id = $1", user_id)
        await rt.cache.set(f"profile:{user_id}", str(name))
        return {"user_id": user_id, "name": name}


ROUTES: dict[str, Callable[[FastAPI, Runtime], None]] = {
    "payment-service": payment_routes,
    "order-service": order_routes,
    "inventory-service": inventory_routes,
    "user-service": user_routes,
}


# --------------------------------------------------------------------------- traffic


async def generate_traffic(
    config: Config, log: JsonLogger, stop: asyncio.Event | None = None
) -> None:
    """Steady synthetic user traffic (TRAFFIC_RPS) across the four services."""
    rng = random.Random()  # noqa: S311 - load generation, not crypto
    stop = stop or asyncio.Event()
    in_flight: set[asyncio.Task[Any]] = set()
    mix: list[tuple[float, str, str, Callable[[], dict[str, Any] | None]]] = [
        (
            0.5,
            "POST",
            config.order_url + "/api/v1/orders",
            lambda: {
                "sku": rng.randint(1000, 9999),
                "quantity": rng.randint(1, 3),
                "user_id": rng.randint(1, 5000),
            },
        ),
        (0.15, "GET", config.inventory_url + "/api/v1/stock/{sku}", lambda: None),
        (0.2, "POST", config.user_url + "/api/v1/login", lambda: {"user_id": rng.randint(1, 5000)}),
        (0.15, "GET", config.user_url + "/api/v1/users/{uid}", lambda: None),
    ]
    log.info(f"Starting traffic-generator at {config.rps} rps", logger="com.acme.traffic.Generator")
    async with httpx.AsyncClient(timeout=15) as client:
        while not stop.is_set():
            _, method, url, body = rng.choices(mix, weights=[m[0] for m in mix])[0]
            target = url.format(sku=rng.randint(1000, 9999), uid=rng.randint(1, 5000))
            if len(in_flight) < 200:
                task = asyncio.create_task(_fire(client, method, target, body()))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
            await asyncio.sleep(rng.expovariate(config.rps))


async def _fire(
    client: httpx.AsyncClient, method: str, url: str, body: dict[str, Any] | None
) -> None:
    with contextlib.suppress(httpx.HTTPError):  # the services log their own failures
        await client.request(method, url, json=body)
