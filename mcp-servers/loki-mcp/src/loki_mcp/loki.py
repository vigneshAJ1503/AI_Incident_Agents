"""Minimal async Loki HTTP API client (GET query/label endpoints only: read-only by construction).

No push, delete, rules or admin endpoints are ever called.
"""

from __future__ import annotations

from typing import Any

import httpx2

from loki_mcp.config import ServerSettings

Params = list[tuple[str, str | int | float | bool | None]]


class LokiError(Exception):
    pass


def request_headers(settings: ServerSettings) -> dict[str, str]:
    """Headers for every Loki API call (bearer auth, multi-tenant org id)."""
    headers = {"Accept": "application/json"}
    if settings.loki_bearer_token:
        headers["Authorization"] = f"Bearer {settings.loki_bearer_token}"
    if settings.loki_org_id:
        headers["X-Scope-OrgID"] = settings.loki_org_id
    return headers


def ns(seconds: float) -> str:
    return str(round(seconds * 1_000_000_000))


class LokiClient:
    def __init__(self, settings: ServerSettings, http: httpx2.AsyncClient | None = None) -> None:
        auth: tuple[str, str] | None = None
        if not settings.loki_bearer_token and settings.loki_username and settings.loki_password:
            auth = (settings.loki_username, settings.loki_password)
        self._http = http or httpx2.AsyncClient(
            base_url=settings.loki_url,
            headers=request_headers(settings),
            auth=auth,
            timeout=settings.query_timeout_s,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: Params | None = None) -> Any:
        try:
            response = await self._http.get(path, params=params or [])
        except httpx2.TimeoutException as exc:
            raise LokiError(f"Loki query timed out: {exc}") from exc
        except httpx2.HTTPError as exc:
            raise LokiError(f"Loki unreachable: {exc}") from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400 or not isinstance(body, dict):
            detail = (
                body.get("error") or body.get("message")
                if isinstance(body, dict)
                else response.text.strip()[:400]
            )
            raise LokiError(f"Loki error {response.status_code}: {detail}")
        if body.get("status") not in (None, "success"):
            raise LokiError(f"Loki error: {body.get('error', 'unknown')}")
        return body.get("data")

    async def query(self, query: str, time: float | None) -> Any:
        params: Params = [("query", query)]
        if time is not None:
            params.append(("time", ns(time)))
        return await self._get("/loki/api/v1/query", params)

    async def query_range(
        self,
        query: str,
        start: float,
        end: float,
        *,
        limit: int,
        direction: str,
        step: int | None,
    ) -> Any:
        params: Params = [
            ("query", query),
            ("start", ns(start)),
            ("end", ns(end)),
            ("limit", limit),
            ("direction", direction),
        ]
        if step is not None:
            params.append(("step", f"{step}s"))
        return await self._get("/loki/api/v1/query_range", params)

    async def labels(self, start: float, end: float, query: str | None) -> list[str]:
        params: Params = [("start", ns(start)), ("end", ns(end))]
        if query:
            params.append(("query", query))
        data = await self._get("/loki/api/v1/labels", params)
        return [str(v) for v in data or []]

    async def label_values(
        self, label: str, start: float, end: float, query: str | None
    ) -> list[str]:
        params: Params = [("start", ns(start)), ("end", ns(end))]
        if query:
            params.append(("query", query))
        data = await self._get(f"/loki/api/v1/label/{label}/values", params)
        return [str(v) for v in data or []]
