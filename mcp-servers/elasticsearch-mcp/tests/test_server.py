from __future__ import annotations

import json
from typing import Any

import httpx2
from mcp import Client

from elasticsearch_mcp.config import ServerSettings
from elasticsearch_mcp.es import ElasticsearchClient
from elasticsearch_mcp.server import create_server

SETTINGS = ServerSettings(allowed_index_patterns=("payment-prod-*",), max_results=2)
START, END = "2026-09-25T10:00:00Z", "2026-09-25T10:30:00Z"


def fake_es(seen: list[httpx2.Request]) -> httpx2.AsyncClient:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        path = request.url.path
        if path == "/_cat/indices":
            return httpx2.Response(
                200,
                json=[
                    {
                        "index": "payment-prod-2026.09.25",
                        "docs.count": "12820",
                        "store.size": "3mb",
                    },
                    {"index": ".security-7", "docs.count": "5", "store.size": "1kb"},
                    {"index": "user-prod-2026.09.25", "docs.count": "10", "store.size": "1kb"},
                ],
            )
        if path.endswith("/_mapping"):
            return httpx2.Response(
                200,
                json={
                    "payment-prod-2026.09.25": {
                        "mappings": {
                            "properties": {
                                "level": {"type": "keyword"},
                                "message": {
                                    "type": "text",
                                    "fields": {"keyword": {"type": "keyword"}},
                                },
                                "http": {"properties": {"status": {"type": "integer"}}},
                            }
                        }
                    }
                },
            )
        if path.endswith("/_search"):
            return httpx2.Response(
                200,
                json={
                    "hits": {
                        "total": {"value": 62},
                        "hits": [{"_source": {"message": "timeout"}}] * 2,
                    }
                },
            )
        if path == "/_query":
            body = json.loads(request.content)
            if "BOOM" in body["query"]:
                return httpx2.Response(
                    400, json={"error": {"root_cause": [{"reason": "Unknown column [BOOM]"}]}}
                )
            return httpx2.Response(
                200,
                json={
                    "columns": [
                        {"name": "n", "type": "long"},
                        {"name": "error_type", "type": "keyword"},
                    ],
                    "values": [[62, "ConnectionTimeoutException"], [1, "X"], [1, "Y"]],
                },
            )
        return httpx2.Response(404, json={"error": "not found"})

    return httpx2.AsyncClient(base_url="http://es", transport=httpx2.MockTransport(handle))


async def call(tool: str, args: dict[str, Any], seen: list[httpx2.Request] | None = None) -> Any:
    seen = seen if seen is not None else []
    server = create_server(SETTINGS, ElasticsearchClient(SETTINGS, fake_es(seen)))
    async with Client(server) as client:
        return await client.call_tool(tool, args)


async def test_tools_listed() -> None:
    server = create_server(SETTINGS, ElasticsearchClient(SETTINGS, fake_es([])))
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"list_indices", "get_mapping", "search_logs", "execute_esql"}


async def test_list_indices_hides_disallowed() -> None:
    result = await call("list_indices", {})
    indices = result.structured_content["indices"]
    assert [i["index"] for i in indices] == ["payment-prod-2026.09.25"]
    assert indices[0]["docs"] == 12820


async def test_get_mapping_flattens() -> None:
    result = await call("get_mapping", {"index": "payment-prod-*"})
    assert result.structured_content["fields"] == {
        "http.status": "integer",
        "level": "keyword",
        "message": "text",
        "message.keyword": "keyword",
    }


async def test_search_logs_builds_bounded_query() -> None:
    seen: list[httpx2.Request] = []
    result = await call(
        "search_logs",
        {
            "index": "payment-prod-*",
            "start": START,
            "end": END,
            "query": "status_code:500",
            "levels": ["error"],
            "size": 50,
        },
        seen,
    )
    data = result.structured_content
    assert data["total"] == 62 and data["returned"] == 2 and data["truncated"] is True
    body = json.loads(seen[0].content)
    assert body["size"] == 2  # clamped to max_results
    filters = body["query"]["bool"]["filter"]
    assert filters[0]["range"]["@timestamp"]["gte"].startswith("2026-09-25T10:00")
    assert filters[1] == {"terms": {"level": ["ERROR"]}}
    assert body["query"]["bool"]["must"][0]["query_string"]["query"] == "status_code:500"


async def test_esql_adds_time_filter_and_truncates() -> None:
    seen: list[httpx2.Request] = []
    result = await call(
        "execute_esql",
        {
            "query": "FROM payment-prod-* | STATS n = COUNT(*) BY error_type",
            "start": START,
            "end": END,
        },
        seen,
    )
    data = result.structured_content
    assert data["columns"] == ["n", "error_type"]
    assert data["rows"] == [[62, "ConnectionTimeoutException"], [1, "X"]]
    assert data["row_count"] == 3 and data["truncated"] is True
    sent = json.loads(seen[0].content)
    assert "range" in sent["filter"]


async def test_guard_violations_are_tool_errors_without_es_calls() -> None:
    seen: list[httpx2.Request] = []
    result = await call(
        "execute_esql", {"query": "FROM user-prod-* | LIMIT 1", "start": START, "end": END}, seen
    )
    assert result.is_error
    assert "outside the allowed patterns" in result.content[0].text
    result = await call("search_logs", {"index": "*", "start": START, "end": END}, seen)
    assert result.is_error
    assert seen == []


async def test_es_errors_surface_reason() -> None:
    result = await call(
        "execute_esql", {"query": "FROM payment-prod-* | KEEP BOOM", "start": START, "end": END}
    )
    assert result.is_error
    assert "Unknown column [BOOM]" in result.content[0].text
