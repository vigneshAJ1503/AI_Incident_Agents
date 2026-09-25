"""MCP tools: list_indices, get_mapping, search_logs, execute_esql (all read-only)."""

from __future__ import annotations

from collections.abc import Awaitable
from fnmatch import fnmatchcase
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from elasticsearch_mcp.config import ServerSettings
from elasticsearch_mcp.es import ElasticsearchClient, ElasticsearchError
from elasticsearch_mcp.guards import (
    GuardError,
    clamp_size,
    validate_esql,
    validate_index,
    validate_time_range,
)

INSTRUCTIONS = """Read-only access to application logs in Elasticsearch.
Every query is limited to allowed index patterns and a bounded time range (ISO-8601 UTC).
Prefer execute_esql for aggregations (counts, top errors, first/last seen) and
search_logs for sample log lines. Always pass start and end."""


def _flatten_mapping(properties: dict[str, Any], prefix: str = "") -> dict[str, str]:
    fields: dict[str, str] = {}
    for name, spec in properties.items():
        path = f"{prefix}{name}"
        if "properties" in spec:
            fields.update(_flatten_mapping(spec["properties"], f"{path}."))
        else:
            fields[path] = spec.get("type", "object")
        for sub, sub_spec in spec.get("fields", {}).items():
            fields[f"{path}.{sub}"] = sub_spec.get("type", "object")
    return fields


def create_server(settings: ServerSettings, es: ElasticsearchClient | None = None) -> MCPServer:
    client = es or ElasticsearchClient(settings)
    server = MCPServer("elasticsearch-mcp", instructions=INSTRUCTIONS, version="0.1.0")
    allowed = settings.allowed_index_patterns

    async def guarded[T](coro: Awaitable[T]) -> T:
        try:
            return await coro
        except ElasticsearchError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    async def list_indices() -> dict[str, Any]:
        """List the log indices you are allowed to query, with document counts and sizes."""
        rows = await guarded(client.cat_indices())
        visible = [
            {
                "index": r["index"],
                "docs": int(r.get("docs.count") or 0),
                "size": r.get("store.size"),
            }
            for r in rows
            if any(fnmatchcase(r["index"], p) for p in allowed)
        ]
        return {
            "allowed_patterns": list(allowed),
            "indices": sorted(visible, key=lambda r: r["index"]),
        }

    @server.tool()
    async def get_mapping(index: str) -> dict[str, Any]:
        """Field names and types for an index pattern, e.g. 'payment-prod-*'."""
        try:
            validate_index(index, allowed)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        mappings = await guarded(client.mapping(index))
        fields: dict[str, str] = {}
        for body in mappings.values():
            fields.update(_flatten_mapping(body.get("mappings", {}).get("properties", {})))
        return {"index": index, "fields": dict(sorted(fields.items()))}

    @server.tool()
    async def search_logs(
        index: str,
        start: str,
        end: str,
        query: str | None = None,
        levels: list[str] | None = None,
        size: int = 20,
        sort: Literal["desc", "asc"] = "desc",
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return sample log documents.

        Args:
            index: allowed index pattern, e.g. 'payment-prod-*'.
            start: window start, ISO-8601 UTC (e.g. '2026-09-25T10:00:00Z').
            end: window end, ISO-8601 UTC.
            query: optional Lucene query, e.g. 'message:"connection timeout" AND status_code:500'.
            levels: optional log levels, e.g. ['ERROR', 'WARN'].
            size: max documents to return (capped by the server).
            sort: 'desc' = newest first, 'asc' = oldest first (use asc to find first occurrence).
            fields: optional list of fields to return.
        """
        try:
            validate_index(index, allowed)
            window = validate_time_range(start, end, settings.max_time_range_hours)
            limit = clamp_size(size, settings.max_results)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        filters: list[dict[str, Any]] = [window.as_filter(settings.timestamp_field)]
        if levels:
            filters.append({"terms": {"level": [lvl.upper() for lvl in levels]}})
        must: list[dict[str, Any]] = []
        if query:
            must.append({"query_string": {"query": query, "default_field": "message"}})
        body: dict[str, Any] = {
            "size": limit,
            "track_total_hits": True,
            "sort": [{settings.timestamp_field: {"order": sort}}],
            "query": {"bool": {"filter": filters, "must": must}},
        }
        if fields:
            body["_source"] = fields
        result = await guarded(client.search(index, body, settings.query_timeout_s))
        hits = [h.get("_source", {}) for h in result.get("hits", {}).get("hits", [])]
        total = result.get("hits", {}).get("total", {}).get("value", len(hits))
        return {
            "total": total,
            "returned": len(hits),
            "truncated": total > len(hits),
            "hits": hits,
        }

    @server.tool()
    async def execute_esql(query: str, start: str, end: str) -> dict[str, Any]:
        """Run a read-only ES|QL query, automatically limited to [start, end].

        The query must start with 'FROM <allowed index pattern>'. Example:
          FROM payment-prod-* | WHERE level == "ERROR"
          | STATS count = COUNT(*), first_seen = MIN(@timestamp) BY error_type | SORT count DESC
        Args:
            query: ES|QL query.
            start: window start, ISO-8601 UTC.
            end: window end, ISO-8601 UTC.
        """
        try:
            validate_esql(query, allowed)
            window = validate_time_range(start, end, settings.max_time_range_hours)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        result = await guarded(client.esql(query, window.as_filter(settings.timestamp_field)))
        columns = [c["name"] for c in result.get("columns", [])]
        values = result.get("values", [])
        rows = values[: settings.max_results]
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(values),
            "truncated": len(values) > len(rows),
            "time_range": {"start": window.start.isoformat(), "end": window.end.isoformat()},
        }

    return server
