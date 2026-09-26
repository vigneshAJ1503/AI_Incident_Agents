"""Minimal async Prometheus HTTP API client (GET endpoints only: read-only by construction)."""

from __future__ import annotations

from typing import Any

import httpx2

from prometheus_mcp.config import ServerSettings

Params = list[tuple[str, str | int | float | bool | None]]


class PrometheusError(Exception):
    pass


class PrometheusClient:
    def __init__(self, settings: ServerSettings, http: httpx2.AsyncClient | None = None) -> None:
        headers = {"Accept": "application/json"}
        auth: tuple[str, str] | None = None
        if settings.prom_bearer_token:
            headers["Authorization"] = f"Bearer {settings.prom_bearer_token}"
        elif settings.prom_username and settings.prom_password:
            auth = (settings.prom_username, settings.prom_password)
        # A little longer than the server-side query timeout, so Prometheus answers first.
        self._http = http or httpx2.AsyncClient(
            base_url=settings.prom_url,
            headers=headers,
            auth=auth,
            timeout=settings.query_timeout_s + 5,
        )
        self._timeout = f"{settings.query_timeout_s:g}s"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: Params | None = None) -> Any:
        try:
            response = await self._http.get(path, params=params or [])
        except httpx2.TimeoutException as exc:
            raise PrometheusError(f"Prometheus query timed out: {exc}") from exc
        except httpx2.HTTPError as exc:
            raise PrometheusError(f"Prometheus unreachable: {exc}") from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400 or not isinstance(body, dict):
            detail = body.get("error") if isinstance(body, dict) else response.text[:300]
            raise PrometheusError(f"Prometheus error {response.status_code}: {detail}")
        if body.get("status") != "success":
            raise PrometheusError(f"Prometheus error: {body.get('error', 'unknown')}")
        return body.get("data")

    async def query(self, query: str, time: float | None) -> Any:
        params: Params = [("query", query), ("timeout", self._timeout)]
        if time is not None:
            params.append(("time", f"{time:.3f}"))
        return await self._get("/api/v1/query", params)

    async def query_range(self, query: str, start: float, end: float, step: int) -> Any:
        return await self._get(
            "/api/v1/query_range",
            [
                ("query", query),
                ("start", f"{start:.3f}"),
                ("end", f"{end:.3f}"),
                ("step", str(step)),
                ("timeout", self._timeout),
            ],
        )

    async def metric_names(self) -> list[str]:
        data = await self._get("/api/v1/label/__name__/values")
        return [str(n) for n in data or []]

    async def metadata(self, metric: str | None, limit: int) -> dict[str, Any]:
        params: Params = [("limit", limit)]
        if metric:
            params.append(("metric", metric))
        data = await self._get("/api/v1/metadata", params)
        return data if isinstance(data, dict) else {}

    async def targets(self, state: str) -> dict[str, Any]:
        data = await self._get("/api/v1/targets", [("state", state)])
        return data if isinstance(data, dict) else {}
