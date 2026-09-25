"""Minimal async Alertmanager API v2 client (GET endpoints only: read-only by construction)."""

from __future__ import annotations

from typing import Any

import httpx2

from alertmanager_mcp.config import ServerSettings

#: Query parameters; a list because ``filter`` repeats.
Params = list[tuple[str, str | int | float | bool | None]]


class AlertmanagerError(Exception):
    pass


class AlertmanagerClient:
    def __init__(self, settings: ServerSettings, http: httpx2.AsyncClient | None = None) -> None:
        headers = {"Accept": "application/json"}
        auth: tuple[str, str] | None = None
        if settings.am_bearer_token:
            headers["Authorization"] = f"Bearer {settings.am_bearer_token}"
        elif settings.am_username and settings.am_password:
            auth = (settings.am_username, settings.am_password)
        self._http = http or httpx2.AsyncClient(
            base_url=settings.am_url, headers=headers, auth=auth, timeout=settings.query_timeout_s
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: Params | None = None) -> Any:
        try:
            response = await self._http.get(path, params=params or [])
        except httpx2.HTTPError as exc:
            raise AlertmanagerError(f"Alertmanager unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise AlertmanagerError(
                f"Alertmanager error {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    async def alerts(
        self, matchers: list[str], *, active: bool, silenced: bool, inhibited: bool
    ) -> list[dict[str, Any]]:
        params = _state_params(matchers, active=active, silenced=silenced, inhibited=inhibited)
        result: list[dict[str, Any]] = await self._get("/api/v2/alerts", params)
        return result

    async def alert_groups(
        self, matchers: list[str], *, active: bool, silenced: bool, inhibited: bool
    ) -> list[dict[str, Any]]:
        params = _state_params(matchers, active=active, silenced=silenced, inhibited=inhibited)
        result: list[dict[str, Any]] = await self._get("/api/v2/alerts/groups", params)
        return result

    async def silences(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._get("/api/v2/silences")
        return result


def _state_params(matchers: list[str], *, active: bool, silenced: bool, inhibited: bool) -> Params:
    def flag(value: bool) -> str:
        return "true" if value else "false"

    return [
        *(("filter", m) for m in matchers),
        ("active", flag(active)),
        ("silenced", flag(silenced)),
        ("inhibited", flag(inhibited)),
        ("unprocessed", flag(active)),  # not yet evaluated = treat like active
    ]
