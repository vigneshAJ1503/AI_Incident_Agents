"""Minimal async Kubernetes API client: GET only, so read-only by construction.

Talks to the REST API directly (no kubectl, no official client: small image and
memory). Credentials are loaded lazily and reloaded once on HTTP 401, so a
regenerated short-lived token (`make k8s-reader-kubeconfig`) is picked up without
restarting the server.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from typing import Any

import httpx2

from kubernetes_mcp.config import ConfigError, KubeCredentials, ServerSettings, load_credentials

Params = dict[str, str | int]


class KubeError(Exception):
    pass


class KubeNotFoundError(KubeError):
    pass


def _ssl_context(creds: KubeCredentials) -> ssl.SSLContext | bool:
    if creds.insecure:
        return False
    if creds.ca_pem:
        return ssl.create_default_context(cadata=creds.ca_pem)
    return True


class KubeClient:
    def __init__(
        self,
        settings: ServerSettings,
        *,
        credentials: Callable[[], KubeCredentials] | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._load = credentials or (lambda: load_credentials(settings))
        self._transport = transport
        self._http: httpx2.AsyncClient | None = None

    def _connect(self) -> httpx2.AsyncClient:
        try:
            creds = self._load()
        except ConfigError as exc:
            raise KubeError(f"Kubernetes credentials unavailable: {exc}") from exc
        return httpx2.AsyncClient(
            base_url=creds.server,
            headers={"Authorization": f"Bearer {creds.token}", "Accept": "application/json"},
            verify=_ssl_context(creds),
            timeout=self._settings.query_timeout_s,
            transport=self._transport,
        )

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _request(self, path: str, params: Params | None) -> httpx2.Response:
        for attempt in (1, 2):
            if self._http is None:
                self._http = self._connect()
            try:
                response = await self._http.get(path, params=params or {})
            except httpx2.HTTPError as exc:
                raise KubeError(f"Kubernetes API unreachable: {exc}") from exc
            if response.status_code == 401 and attempt == 1:
                await self.aclose()  # token expired or rotated: reload credentials once
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    async def _checked(self, path: str, params: Params | None) -> httpx2.Response:
        response = await self._request(path, params)
        if response.status_code == 404:
            raise KubeNotFoundError(f"not found: {path}")
        if response.status_code == 403:
            raise KubeError(
                f"forbidden by RBAC (the ServiceAccount is read-only): {_reason(response)}"
            )
        if response.status_code == 401:
            raise KubeError(
                "unauthorized: the ServiceAccount token expired or is invalid "
                "(regenerate it: make k8s-reader-kubeconfig)"
            )
        if response.status_code >= 400:
            raise KubeError(f"Kubernetes API error {response.status_code}: {_reason(response)}")
        return response

    async def get_json(self, path: str, params: Params | None = None) -> dict[str, Any]:
        response = await self._checked(path, params)
        data: dict[str, Any] = response.json()
        return data

    async def get_text(self, path: str, params: Params | None = None) -> str:
        return (await self._checked(path, params)).text


def _reason(response: httpx2.Response) -> str:
    try:
        return str(response.json().get("message", ""))[:300]
    except ValueError:
        return response.text[:300]
