from __future__ import annotations

from typing import Any

import httpx2
from mcp import Client

from prometheus_mcp.config import ServerSettings
from prometheus_mcp.prom import PrometheusClient
from prometheus_mcp.server import create_server, number

SETTINGS = ServerSettings(max_range_hours=6, max_points=200, max_series=2, max_results=3)
T0 = 1790330400  # 2026-09-25T10:00:00Z

METRICS = ["db_pool_connections_active", "http_requests_total", "redis_up", "up"]


def series(service: str, values: list[tuple[int, str]]) -> dict[str, Any]:
    return {"metric": {"service": service}, "values": [[t, v] for t, v in values]}


def fake_prom(seen: list[httpx2.Request]) -> httpx2.AsyncClient:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        assert request.method == "GET"  # read-only
        path, params = request.url.path, request.url.params
        if path == "/api/v1/query":
            if "boom" in params["query"]:
                return httpx2.Response(
                    422, json={"status": "error", "error": "parse error: unexpected"}
                )
            return ok(
                {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"service": "b"}, "value": [T0, "2.5"]},
                        {"metric": {"service": "a"}, "value": [T0, "NaN"]},
                        {"metric": {"service": "c"}, "value": [T0, "0.123456789"]},
                    ],
                }
            )
        if path == "/api/v1/query_range":
            return ok(
                {
                    "resultType": "matrix",
                    "result": [
                        series("payment-service", [(T0, "0.01"), (T0 + 60, "+Inf")]),
                    ],
                }
            )
        if path == "/api/v1/label/__name__/values":
            return ok(METRICS)
        if path == "/api/v1/metadata":
            return ok(
                {
                    "http_requests_total": [
                        {"type": "counter", "help": "HTTP requests", "unit": ""}
                    ],
                    "up": [{"type": "gauge", "help": "scrape ok", "unit": ""}],
                }
            )
        if path == "/api/v1/targets":
            return ok(
                {
                    "activeTargets": [
                        {
                            "scrapePool": "sample-services",
                            "scrapeUrl": "http://172.21.0.100:30081/metrics",
                            "labels": {"job": "sample-services", "instance": "172.21.0.100:30081"},
                            "health": "down",
                            "lastError": "connection refused",
                            "lastScrape": "2026-09-25T10:00:00Z",
                            "lastScrapeDuration": 0.01,
                        },
                        {
                            "scrapePool": "kube-state-metrics",
                            "scrapeUrl": "http://172.21.0.100:30080/metrics",
                            "labels": {"job": "kube-state-metrics", "instance": "x:30080"},
                            "health": "up",
                            "lastError": "",
                        },
                    ],
                    "droppedTargets": [],
                }
            )
        return httpx2.Response(404, text="not found")

    return httpx2.AsyncClient(base_url="http://prom", transport=httpx2.MockTransport(handle))


def ok(data: Any) -> httpx2.Response:
    return httpx2.Response(200, json={"status": "success", "data": data})


async def call(
    tool: str,
    args: dict[str, Any],
    seen: list[httpx2.Request] | None = None,
    settings: ServerSettings = SETTINGS,
) -> Any:
    seen = seen if seen is not None else []
    server = create_server(settings, PrometheusClient(settings, fake_prom(seen)))
    async with Client(server) as client:
        return await client.call_tool(tool, args)


async def test_tools_listed() -> None:
    server = create_server(SETTINGS, PrometheusClient(SETTINGS, fake_prom([])))
    async with Client(server) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
    assert tools == {"query", "query_range", "list_metrics", "metric_metadata", "get_targets"}


async def test_query_shapes_sorts_and_caps_series() -> None:
    seen: list[httpx2.Request] = []
    result = await call(
        "query",
        {
            "query": "sum by (service) (rate(http_requests_total[5m]))",
            "time": "2026-09-25T10:00:00Z",
        },
        seen,
    )
    data = result.structured_content
    params = seen[0].url.params
    assert params["time"] == f"{T0:.3f}" and params["timeout"] == "20s"
    assert data["metrics"] == ["http_requests_total"]
    assert data["series_total"] == 3 and data["returned"] == 2 and data["truncated"] is True
    assert [s["labels"]["service"] for s in data["series"]] == ["a", "b"]  # sorted
    assert data["series"][0]["value"] is None  # NaN is not JSON
    assert data["series"][1]["value"] == 2.5


async def test_query_range_auto_step_and_values() -> None:
    seen: list[httpx2.Request] = []
    result = await call(
        "query_range",
        {
            "query": 'sum(rate(http_requests_total{service="payment-service"}[5m]))',
            "start": "2026-09-25T10:00:00Z",
            "end": "2026-09-25T11:00:00Z",
        },
        seen,
    )
    data = result.structured_content
    assert seen[0].url.params["step"] == "19"  # ceil(3600 / 199)
    assert data["step_s"] == 19 and data["start"] == "2026-09-25T10:00:00Z"
    assert data["series"][0]["values"] == [[T0, 0.01], [T0 + 60, None]]


async def test_guardrails_reject_before_calling_prometheus() -> None:
    seen: list[httpx2.Request] = []
    bad: list[tuple[str, dict[str, Any], str]] = [
        ("query", {"query": '{job=~".+"}'}, "needs a metric name"),
        ("query", {"query": "up", "time": "noon"}, "ISO-8601"),
        (
            "query_range",
            {"query": "up", "start": "2026-09-25T00:00:00Z", "end": "2026-09-25T10:00:00Z"},
            "exceeds the maximum of 6 hours",
        ),
        (
            "query_range",
            {
                "query": "up",
                "start": "2026-09-25T10:00:00Z",
                "end": "2026-09-25T11:00:00Z",
                "step": "15s",
            },
            "points per series exceeds the maximum of 200",
        ),
        ("query", {"query": "rate(up[7d])"}, "exceeds the maximum"),
        ("metric_metadata", {"metric": "bad name"}, "invalid metric name"),
        ("list_metrics", {"limit": 0}, "limit must be >= 1"),
        ("get_targets", {"state": "all"}, "Input should be"),
    ]
    for tool, args, message in bad:
        result = await call(tool, args, seen)
        assert result.is_error, (tool, args)
        assert message in result.content[0].text, (tool, result.content[0].text)
    assert seen == []


async def test_prometheus_errors_surface() -> None:
    result = await call("query", {"query": "boom_metric"})
    assert result.is_error and "parse error" in result.content[0].text

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    http = httpx2.AsyncClient(base_url="http://prom", transport=httpx2.MockTransport(refuse))
    server = create_server(SETTINGS, PrometheusClient(SETTINGS, http))
    async with Client(server) as client:
        result = await client.call_tool("query", {"query": "up"})
    assert result.is_error and "unreachable" in result.content[0].text


async def test_list_metrics_filter_cap_and_allowlist() -> None:
    result = await call("list_metrics", {"match": "HTTP"})
    assert result.structured_content["metrics"] == ["http_requests_total"]
    capped = await call("list_metrics", {})
    assert capped.structured_content["total"] == 4 and capped.structured_content["truncated"]
    allow = ServerSettings(metric_allowlist=("http_.*", "redis_up"))
    listed = await call("list_metrics", {}, settings=allow)
    assert listed.structured_content["metrics"] == ["http_requests_total", "redis_up"]
    blocked = await call("query", {"query": "up"}, settings=allow)
    assert blocked.is_error and "allowlist" in blocked.content[0].text
    meta = await call("metric_metadata", {}, settings=allow)
    assert [m["metric"] for m in meta.structured_content["metadata"]] == ["http_requests_total"]


async def test_metadata_and_targets() -> None:
    seen: list[httpx2.Request] = []
    meta = await call("metric_metadata", {"metric": "http_requests_total"}, seen)
    assert seen[0].url.params["metric"] == "http_requests_total"
    assert meta.structured_content["metadata"][0]["type"] == "counter"
    targets = (await call("get_targets", {})).structured_content
    assert targets["total"] == 2 and targets["down"] == 1
    assert targets["targets"][0]["job"] == "kube-state-metrics"  # sorted by job
    assert targets["targets"][1]["last_error"] == "connection refused"


def test_number() -> None:
    assert number("1.23456789") == 1.23457
    assert number("NaN") is None and number("-Inf") is None and number("x") is None
