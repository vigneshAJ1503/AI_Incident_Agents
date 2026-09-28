"""The FastAPI app (PR-035): REST + SSE over the orchestrator for the Web UI.

Run it with ``aiops serve`` (or ``make api``). The profile comes from ``AIOPS_PROFILE``;
nothing here is vendor-specific. OpenAPI: ``/api/docs`` and ``/api/openapi.json``.

Middleware, outermost first: CORS (``api.cors_origins``, exact origins only) -> request id
+ structured access log -> security headers -> request-size limit (``api.max_body_bytes``)
-> authentication (``api.auth``: none | api_key; OIDC in PR-045, see ``api/auth.py``).
Rate limits and idempotency keys are per route (``api/security.py``).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aiops import __version__
from aiops.api.auth import PUBLIC_PATHS, AuthMiddleware, build_authenticator
from aiops.api.context import ApiContext, build_context
from aiops.api.errors import install_error_handlers
from aiops.api.routes import router
from aiops.api.security import (
    BodySizeLimitMiddleware,
    IdempotencyStore,
    RateLimiter,
    SecurityHeadersMiddleware,
)

access_log = logging.getLogger("aiops.api.access")

REQUEST_ID_HEADER = "x-request-id"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
__all__ = ["PUBLIC_PATHS", "RequestContextMiddleware", "create_app"]


class RequestContextMiddleware:
    """``X-Request-ID`` (kept when the caller sends a sane one) + one JSON access-log line
    per request (the path only: query strings may carry ``api_key``)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(REQUEST_ID_HEADER.encode(), b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [
                    *message["headers"],
                    (REQUEST_ID_HEADER.encode(), request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            client = scope.get("client")
            access_log.info(
                json.dumps(
                    {
                        "event": "http_request",
                        "request_id": request_id,
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        "client": client[0] if client else None,
                    }
                )
            )


def create_app(ctx: ApiContext | None = None) -> FastAPI:
    """The app; ``ctx`` defaults to the production wiring of ``$AIOPS_PROFILE``."""
    context = ctx or build_context()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await context.startup()
        try:
            yield
        finally:
            await context.shutdown()

    app = FastAPI(
        title="AI Incident Agents API",
        version=__version__,
        description="REST + SSE over the investigation orchestrator (docs/api/contract.md).",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.ctx = context
    api = context.settings.api
    app.state.limiter = RateLimiter.from_config(api)
    app.state.idempotency = IdempotencyStore(api.idempotency_ttl_s)
    install_error_handlers(app)
    app.include_router(router)

    app.add_middleware(AuthMiddleware, authenticator=build_authenticator(api))
    app.add_middleware(BodySizeLimitMiddleware, limit=api.max_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    cors: dict[str, Any] = {
        "allow_origins": list(api.cors_origins),
        "allow_methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": [
            "Content-Type",
            "X-API-Key",
            "Last-Event-ID",
            "X-Request-ID",
            "Idempotency-Key",
        ],
        "allow_credentials": False,
        "expose_headers": ["X-Request-ID", "Retry-After", "Idempotent-Replayed"],
        "max_age": 600,
    }
    app.add_middleware(CORSMiddleware, **cors)
    return app
