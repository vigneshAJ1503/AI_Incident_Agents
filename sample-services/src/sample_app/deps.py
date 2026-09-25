"""Backends with the failure behaviour the scenarios rely on.

- Database: a bounded connection pool. When no connection frees up within DB_TIMEOUT_MS,
  it raises PoolTimeoutError, the S1 incident when DB_POOL_SIZE is too small.
- Cache: Redis with fallback. Connection errors are reported, never raised (S5).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

import redis.asyncio as aioredis
from redis.exceptions import RedisError


class PoolTimeoutError(Exception):
    def __init__(self, timeout_ms: int, size: int, active: int, waiting: int) -> None:
        super().__init__(
            f"Database connection timeout: could not acquire a connection from the pool within "
            f"{timeout_ms}ms (pool size={size}, active={active}, waiting={waiting})"
        )


class Connection(Protocol):
    async def execute(self, query: str, *args: Any) -> str: ...
    async def fetchval(self, query: str, *args: Any) -> Any: ...


class Database:
    """Wraps an asyncpg pool; tracks active/waiting for metrics and error messages."""

    def __init__(self, pool: Any, size: int, timeout_ms: int) -> None:
        self._pool = pool
        self.size = size
        self.timeout_ms = timeout_ms
        self.active = 0
        self.waiting = 0

    @classmethod
    async def connect(cls, dsn: str, size: int, timeout_ms: int) -> Database:
        import asyncpg

        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=size, command_timeout=30)
        return cls(pool, size, timeout_ms)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
        self.waiting += 1
        try:
            async with asyncio.timeout(self.timeout_ms / 1000):
                conn = await self._pool.acquire()
        except TimeoutError:
            raise PoolTimeoutError(
                self.timeout_ms, self.size, self.active, self.waiting - 1
            ) from None
        finally:
            self.waiting -= 1
        self.active += 1
        try:
            yield conn
        finally:
            self.active -= 1
            await self._pool.release(conn)

    async def close(self) -> None:
        await self._pool.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (id BIGSERIAL PRIMARY KEY, order_id BIGINT, amount NUMERIC, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS orders (id BIGSERIAL PRIMARY KEY, sku INT, quantity INT, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS stock_levels (sku INT PRIMARY KEY, quantity INT NOT NULL);
CREATE TABLE IF NOT EXISTS users (id INT PRIMARY KEY, name TEXT NOT NULL);
INSERT INTO stock_levels SELECT g, 1000 FROM generate_series(1000, 9999) g ON CONFLICT DO NOTHING;
INSERT INTO users SELECT g, 'user-' || g FROM generate_series(1, 5000) g ON CONFLICT DO NOTHING;
"""


class Cache:
    """Redis cache that never raises: callers learn about failures via the return value."""

    def __init__(self, client: Any | None) -> None:
        self._client = client
        self.healthy = client is not None
        self.last_error: str | None = None

    @classmethod
    def from_url(cls, url: str | None) -> Cache:
        if not url:
            return cls(None)
        return cls(aioredis.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5))

    async def get(self, key: str) -> tuple[bool, str | None]:
        """Returns (ok, value)."""
        if self._client is None:
            return False, None
        try:
            value = await self._client.get(key)
        except (RedisError, OSError) as exc:
            return self._failed(exc), None
        self.healthy = True
        return True, value.decode() if isinstance(value, bytes) else value

    async def set(self, key: str, value: str, ttl_s: int = 300) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.set(key, value, ex=ttl_s)
        except (RedisError, OSError) as exc:
            return self._failed(exc)
        self.healthy = True
        return True

    def _failed(self, exc: Exception) -> bool:
        self.healthy = False
        self.last_error = str(exc)
        return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
