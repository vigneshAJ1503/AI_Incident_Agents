"""HTTP hardening for the API (PR-042): security headers, a request-size limit, per-client
rate limits and idempotency keys. All in-process (one API process per deployment today;
a shared store, e.g. Redis/Postgres, is the multi-replica follow-up).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aiops.core.config import ApiConfig, parse_rate

# --------------------------------------------------------------------------- headers

#: The API serves JSON/SSE only: nothing may be framed, run or loaded from its responses.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
#: Swagger UI (/api/docs) loads its bundle from jsDelivr and runs one inline bootstrap.
DOCS_CSP = (
    "default-src 'none'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; img-src 'self' data: "
    "https://fastapi.tiangolo.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)
DOCS_PATHS = {"/api/docs", "/api/docs/oauth2-redirect"}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        csp = DOCS_CSP if scope.get("path") in DOCS_PATHS else API_CSP

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", csp.encode()),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                ]
                if b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-store"))
                message["headers"] = headers + [h for h in extra if h[0] not in present]
            await send(message)

        await self.app(scope, receive, send_wrapper)


# --------------------------------------------------------------------------- body size


class BodySizeLimitMiddleware:
    """413 for bodies over ``limit`` bytes: by ``Content-Length`` up front; a body without
    one (chunked) is read, up to the limit, before the app sees it (bodies are small)."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.limit
            except ValueError:
                too_big = True
            if too_big:
                await self._reject(send)
                return
            await self.app(scope, receive, send)
            return
        if b"transfer-encoding" not in headers:
            await self.app(scope, receive, send)  # no body (GET, SSE...)
            return
        buffered: list[Message] = []
        size = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            size += len(message.get("body", b""))
            if size > self.limit:
                await self._reject(send)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            return buffered.pop(0) if buffered else await receive()

        await self.app(scope, replay, send)

    async def _reject(self, send: Send) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": "payload_too_large",
                    "message": f"Request body over {self.limit} bytes.",
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------- rate limits


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Token buckets per (group, client): ``N/period`` allows bursts of N, refilled evenly."""

    MAX_CLIENTS = 10_000

    def __init__(self, rates: dict[str, str], clock: Any = time.monotonic) -> None:
        self._rates = {group: parse_rate(rate) for group, rate in rates.items()}
        self._clock = clock
        self._buckets: OrderedDict[tuple[str, str], _Bucket] = OrderedDict()

    @classmethod
    def from_config(cls, api: ApiConfig) -> RateLimiter:
        return cls(dict(api.rate_limits))

    def check(self, group: str, client: str) -> float | None:
        """Take one token; ``None`` = allowed, else seconds until the next token."""
        rate = self._rates.get(group)
        if rate is None:
            return None
        capacity, period = rate
        now = self._clock()
        key = (group, client)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(capacity), updated=now)
            self._buckets[key] = bucket
            if len(self._buckets) > self.MAX_CLIENTS:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
        refill = (now - bucket.updated) * capacity / period
        bucket.tokens = min(float(capacity), bucket.tokens + refill)
        bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return None
        return (1.0 - bucket.tokens) * period / capacity

    def describe(self, group: str) -> str:
        capacity, period = self._rates[group]
        return f"{capacity} per {period:.0f}s"


def retry_after(seconds: float) -> str:
    return str(max(1, math.ceil(seconds)))


# --------------------------------------------------------------------------- idempotency

IDEMPOTENCY_HEADER = "Idempotency-Key"
_VALID_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


@dataclass
class _Remembered:
    fingerprint: str
    response: dict[str, Any]
    expires: float


class IdempotencyStore:
    """``(client, Idempotency-Key)`` -> the first response, for ``ttl_s``. A retry with the
    same key and body gets the same investigation; a different body is refused (422)."""

    MAX_KEYS = 10_000

    def __init__(self, ttl_s: float, clock: Any = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._items: OrderedDict[tuple[str, str], _Remembered] = OrderedDict()
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    @staticmethod
    def valid(key: str) -> bool:
        return bool(_VALID_KEY.match(key))

    @staticmethod
    def fingerprint(body: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def lock(self, client: str, key: str) -> asyncio.Lock:
        return self._locks.setdefault((client, key), asyncio.Lock())

    def get(self, client: str, key: str) -> _Remembered | None:
        self._expire()
        return self._items.get((client, key))

    def remember(self, client: str, key: str, fingerprint: str, response: dict[str, Any]) -> None:
        self._items[(client, key)] = _Remembered(fingerprint, response, self._clock() + self.ttl_s)
        while len(self._items) > self.MAX_KEYS:
            old, _ = self._items.popitem(last=False)
            self._locks.pop(old, None)

    def _expire(self) -> None:
        now = self._clock()
        for item_key in [k for k, v in self._items.items() if v.expires <= now]:
            self._items.pop(item_key, None)
            self._locks.pop(item_key, None)
