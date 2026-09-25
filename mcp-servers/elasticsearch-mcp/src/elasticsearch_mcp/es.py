"""Minimal async Elasticsearch REST client (read-only operations only)."""

from __future__ import annotations

from typing import Any

import httpx2

from elasticsearch_mcp.config import ServerSettings


class ElasticsearchError(Exception):
    pass


def _reason(response: httpx2.Response) -> str:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return response.text[:300]
    if isinstance(error, dict):
        root = (error.get("root_cause") or [{}])[0]
        return str(root.get("reason") or error.get("reason") or error)[:500]
    return str(error)[:500]


class ElasticsearchClient:
    def __init__(self, settings: ServerSettings, http: httpx2.AsyncClient | None = None) -> None:
        headers = {"Content-Type": "application/json"}
        auth: tuple[str, str] | None = None
        if settings.es_api_key:
            headers["Authorization"] = f"ApiKey {settings.es_api_key}"
        elif settings.es_username and settings.es_password:
            auth = (settings.es_username, settings.es_password)
        self._http = http or httpx2.AsyncClient(
            base_url=settings.es_url, headers=headers, auth=auth, timeout=settings.query_timeout_s
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx2.HTTPError as exc:
            raise ElasticsearchError(f"Elasticsearch unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise ElasticsearchError(
                f"Elasticsearch error {response.status_code}: {_reason(response)}"
            )
        return response.json()

    async def cat_indices(self) -> list[dict[str, Any]]:
        params = {"format": "json", "h": "index,docs.count,store.size", "expand_wildcards": "open"}
        result: list[dict[str, Any]] = await self._request("GET", "/_cat/indices", params=params)
        return result

    async def mapping(self, index: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("GET", f"/{index}/_mapping")
        return result

    async def search(self, index: str, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        params = {"timeout": f"{int(timeout_s)}s", "ignore_unavailable": "true"}
        result: dict[str, Any] = await self._request(
            "POST", f"/{index}/_search", params=params, json=body
        )
        return result

    async def esql(self, query: str, filter_: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(
            "POST", "/_query", json={"query": query, "filter": filter_}
        )
        return result
