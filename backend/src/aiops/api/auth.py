"""Authentication for the API (PR-042), behind one interface that OIDC plugs into (PR-045).

    api.auth: auto | none | api_key | oidc      (auto = api_key when a key is configured)

* ``none``: local development. Every caller is the anonymous principal; approvals keep the
  self-declared ``by`` of the request (the demo's behaviour).
* ``api_key``: ``X-API-Key`` on every /api route except ``PUBLIC_PATHS`` (SSE may pass
  ``?api_key=``: ``EventSource`` can't set headers). ``api.api_key`` is a *shared* secret
  (no identity); ``api.api_keys`` are *named* keys (``alice:KEY``) and each is an identity.
  Approvals need an identity: they are recorded as decided by the key's name.
* ``oidc``: reserved. ``OIDCAuthenticator`` shows the contract (bearer token -> Principal
  with subject, tenant and roles); config validation refuses it until PR-045 lands.

The middleware stores the principal in ``scope["state"]["principal"]``; handlers read it
with :func:`principal_of`.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from aiops.core.config import ApiConfig, OIDCConfig

#: Reachable without credentials (liveness probes, the docs).
PUBLIC_PATHS = {"/api/health", "/api/docs", "/api/openapi.json", "/api/docs/oauth2-redirect"}


@dataclass(frozen=True)
class Principal:
    """Who is calling. ``identified`` = a named, authenticated person or service."""

    subject: str
    method: str  # none | api_key | oidc
    identified: bool = False
    tenant: str | None = None  # PR-045
    roles: frozenset[str] = field(default_factory=frozenset)

    @property
    def authenticated(self) -> bool:
        return self.method != "none"


ANONYMOUS = Principal(subject="anonymous", method="none")


class AuthError(Exception):
    """Missing or invalid credentials (HTTP 401)."""


class Authenticator(Protocol):
    mode: str

    def authenticate(self, scope: Scope) -> Principal:
        """The caller of this request, or raise AuthError."""
        ...


def _header(scope: Scope, name: bytes) -> bytes:
    for key, value in scope.get("headers") or []:
        if key == name:
            return bytes(value)
    return b""


class NoAuth:
    mode = "none"

    def authenticate(self, scope: Scope) -> Principal:
        return ANONYMOUS


class ApiKeyAuthenticator:
    """Constant-time comparison against the shared key and every named key."""

    mode = "api_key"

    def __init__(self, shared: str | None, named: dict[str, str]) -> None:
        self._keys: list[tuple[bytes, Principal]] = []
        if shared:
            self._keys.append((shared.encode(), Principal("api-key", "api_key")))
        for name, key in sorted(named.items()):
            if key:
                self._keys.append((key.encode(), Principal(name, "api_key", identified=True)))

    def authenticate(self, scope: Scope) -> Principal:
        supplied = _header(scope, b"x-api-key")
        path = str(scope.get("path", ""))
        if not supplied and path.endswith("/events"):
            query = parse_qs(bytes(scope.get("query_string", b"")).decode("latin-1"))
            supplied = (query.get("api_key") or [""])[0].encode()
        if not supplied:
            raise AuthError("Missing X-API-Key.")
        found: Principal | None = None
        for key, principal in self._keys:  # no early exit: every key is compared
            if hmac.compare_digest(supplied, key) and found is None:
                found = principal
        if found is None:
            raise AuthError("Invalid X-API-Key.")
        return found


class OIDCAuthenticator:
    """PR-045. ``Authorization: Bearer <JWT>`` -> verify signature (JWKS), ``iss``, ``aud``,
    ``exp`` -> ``Principal(subject=claims[subject_claim], method="oidc", identified=True,
    tenant=claims.get("tenant"), roles=claims[roles_claim])``."""

    mode = "oidc"

    def __init__(self, config: OIDCConfig) -> None:
        self.config = config

    def authenticate(self, scope: Scope) -> Principal:  # pragma: no cover - PR-045
        raise NotImplementedError("OIDC authentication arrives with PR-045")


def build_authenticator(api: ApiConfig) -> Authenticator:
    mode = api.auth_mode
    if mode == "api_key":
        shared = api.api_key.get_secret_value() if api.api_key else None
        named = {n: k.get_secret_value() for n, k in api.api_keys.items()}
        return ApiKeyAuthenticator(shared, named)
    if mode == "oidc":  # refused by ApiConfig validation until PR-045
        if api.oidc is None:
            raise ValueError("api.auth 'oidc' needs api.oidc")
        return OIDCAuthenticator(api.oidc)
    return NoAuth()


class AuthMiddleware:
    """Resolves the principal of every /api request; 401 when credentials are required
    and missing/invalid. OPTIONS (CORS preflight) and PUBLIC_PATHS pass."""

    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        self.app = app
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        path = str(scope.get("path", ""))
        if scope.get("method") == "OPTIONS" or not path.startswith("/api") or path in PUBLIC_PATHS:
            state["principal"] = ANONYMOUS
            await self.app(scope, receive, send)
            return
        try:
            state["principal"] = self.authenticator.authenticate(scope)
        except AuthError as exc:
            await _unauthorized(send, str(exc))
            return
        await self.app(scope, receive, send)


async def _unauthorized(send: Send, message: str) -> None:
    body = json.dumps({"error": {"code": "unauthorized", "message": message}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'ApiKey header="X-API-Key"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def principal_of(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, Principal) else ANONYMOUS
