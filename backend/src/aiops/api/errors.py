"""The contract's error shape: ``{"error": {"code": "...", "message": "..."}}``."""

from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from aiops.core.config import ConfigError
from aiops.store.db import StoreError

log = logging.getLogger("aiops.api")

_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "too_many_requests",
    503: "unavailable",
}


class ApiError(Exception):
    """An expected failure with an HTTP status, a stable code and a human message."""

    def __init__(
        self, status: int, code: str, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def not_found(what: str, ident: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} '{ident}' not found")


def error_response(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers or None
    )


def _validation_message(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        where = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        parts.append(f"{where or 'body'}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid request"


def install_error_handlers(app: FastAPI) -> None:
    async def api_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)  # noqa: S101 (registered for ApiError only)
        return error_response(exc.status, exc.code, exc.message, exc.headers)

    async def http_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)  # noqa: S101
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        detail: Any = exc.detail
        return error_response(exc.status_code, code, str(detail))

    async def validation_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)  # noqa: S101
        return error_response(422, "validation_error", _validation_message(exc))

    async def store_error(_: Request, exc: Exception) -> JSONResponse:
        log.warning("store unavailable: %s", exc)
        return error_response(
            503,
            "store_unavailable",
            "The evidence store (Postgres) is unavailable. Is the stack up (make infra-up)?",
        )

    async def config_error(_: Request, exc: Exception) -> JSONResponse:
        return error_response(500, "configuration_error", str(exc))

    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return error_response(500, "internal_error", "Internal error; see the API logs.")

    app.add_exception_handler(ApiError, api_error)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(StoreError, store_error)
    app.add_exception_handler(sa.exc.OperationalError, store_error)
    app.add_exception_handler(sa.exc.InterfaceError, store_error)
    app.add_exception_handler(ConfigError, config_error)
    app.add_exception_handler(Exception, unexpected)
