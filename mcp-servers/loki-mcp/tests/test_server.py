from __future__ import annotations

from typing import Any

import httpx2
from mcp import Client

from loki_mcp.config import ServerSettings
from loki_mcp.loki import LokiClient, request_headers
from loki_mcp.server import create_server, number

SETTINGS = ServerSettings(max_range_hours=6, max_lines=3, max_series=2, max_results=2)
T0 = 1790330400  # 2026-09-25T10:00:00Z
NS = 1_000_000_000

SEL = '{namespace="prod", app="payment-service"}'


def fake_loki(seen: list[httpx2.Request]) -> httpx2.AsyncClient:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        assert request.method == "GET"  # read-only
        path, params = request.url.path, request.url.params
        if path == "/loki/api/v1/query":
            if "boom" in params["query"]:
                return httpx2.Response(400, text="parse error at line 1, col 5: syntax error\n")
            return ok(
                {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"level": "INFO", "stat": "count"}, "value": [T0, "1975"]},
                        {
                            "metric": {"level": "ERROR", "stat": "first_seen"},
                            "value": [T0, "1790330533719"],
                        },
                        {"metric": {"level": "ERROR", "stat": "count"}, "value": [T0, "45"]},
                    ],
                }
            )
        if path == "/loki/api/v1/query_range":
            if params["query"].startswith("{"):
                return ok(
                    {
                        "resultType": "streams",
                        "result": [
                            {
                                "stream": {"app": "payment-service", "trace_id": "b"},
                                "values": [[str((T0 + 2) * NS), '{"message":"b"}']],
                            },
                            {
                                "stream": {"app": "payment-service", "trace_id": "a"},
                                "values": [
                                    [str((T0 + 1) * NS + 5_000_000), '{"message":"a"}'],
                                    [str((T0 + 3) * NS), '{"message":"c"}'],
                                ],
                            },
                        ],
                    }
                )
            return ok(
                {
                    "resultType": "matrix",
                    "result": [
                        {"metric": {"level": "ERROR"}, "values": [[T0, "3"], [T0 + 60, "0.5"]]}
                    ],
                }
            )
        if path == "/loki/api/v1/labels":
            return ok(["app", "level", "namespace"])
        if path.startswith("/loki/api/v1/label/"):
            return ok(["payment-service", "order-service"])
        return httpx2.Response(404, text="404 page not found")

    return httpx2.AsyncClient(base_url="http://loki", transport=httpx2.MockTransport(handle))


def ok(data: Any) -> httpx2.Response:
    return httpx2.Response(200, json={"status": "success", "data": data})


async def call(
    tool: str,
    args: dict[str, Any],
    seen: list[httpx2.Request] | None = None,
    settings: ServerSettings = SETTINGS,
) -> Any:
    seen = seen if seen is not None else []
    server = create_server(settings, LokiClient(settings, fake_loki(seen)))
    async with Client(server) as client:
        return await client.call_tool(tool, args)


async def test_tools_listed() -> None:
    server = create_server(SETTINGS, LokiClient(SETTINGS, fake_loki([])))
    async with Client(server) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
    assert tools == {"query", "query_range", "list_labels", "label_values"}


async def test_query_keeps_exact_integers_and_sorts() -> None:
    seen: list[httpx2.Request] = []
    result = await call(
        "query",
        {
            "query": f"sum by (level) (count_over_time({SEL} [15m]))",
            "time": "2026-09-25T10:00:00Z",
        },
        seen,
    )
    data = result.structured_content
    assert seen[0].url.params["time"] == str(T0 * NS)
    assert data["series_total"] == 3 and data["returned"] == 2 and data["truncated"] is True
    values = {(s["labels"]["level"], s["labels"]["stat"]): s["value"] for s in data["series"]}
    assert values == {("ERROR", "count"): 45, ("ERROR", "first_seen"): 1790330533719}


async def test_query_range_log_lines_sorted_and_capped() -> None:
    seen: list[httpx2.Request] = []
    result = await call(
        "query_range",
        {
            "query": f'{SEL} |= "timeout"',
            "start": "2026-09-25T10:00:00Z",
            "end": "2026-09-25T11:00:00Z",
            "limit": 10,
            "direction": "forward",
        },
        seen,
    )
    data = result.structured_content
    params = seen[0].url.params
    assert params["limit"] == "3" and params["direction"] == "forward"  # capped by MAX_LINES
    assert params["start"] == str(T0 * NS) and "step" not in params
    assert [line["timestamp"] for line in data["lines"]] == [
        "2026-09-25T10:00:01.005Z",
        "2026-09-25T10:00:02.000Z",
        "2026-09-25T10:00:03.000Z",
    ]
    assert data["lines"][0]["labels"]["trace_id"] == "a"
    assert data["truncated"] is True  # a full page: there may be more


async def test_query_range_metric_series() -> None:
    seen: list[httpx2.Request] = []
    result = await call(
        "query_range",
        {
            "query": f"sum by (level) (count_over_time({SEL} [1m]))",
            "start": "2026-09-25T10:00:00Z",
            "end": "2026-09-25T11:00:00Z",
            "step": "60s",
        },
        seen,
    )
    data = result.structured_content
    assert seen[0].url.params["step"] == "60s" and data["step_s"] == 60
    assert data["series"][0]["values"] == [[T0, 3], [T0 + 60, 0.5]]


async def test_guardrails_reject_before_calling_loki() -> None:
    seen: list[httpx2.Request] = []
    bad: list[tuple[str, dict[str, Any], str]] = [
        ("query", {"query": 'count_over_time({app="x"} [5m])'}, "must include one of"),
        ("query", {"query": SEL}, "takes a metric query"),
        ("query", {"query": f"count_over_time({SEL} [5m])", "time": "noon"}, "ISO-8601"),
        (
            "query_range",
            {"query": SEL, "start": "2026-09-25T00:00:00Z", "end": "2026-09-25T10:00:00Z"},
            "exceeds the maximum of 6 hours",
        ),
        (
            "query_range",
            {
                "query": SEL,
                "start": "2026-09-25T10:00:00Z",
                "end": "2026-09-25T11:00:00Z",
                "limit": 0,
            },
            "limit must be >= 1",
        ),
        ("label_values", {"label": "bad-name"}, "invalid label name"),
        ("label_values", {"label": "app", "selector": '{namespace="kube-system"}'}, "must include"),
        ("list_labels", {"selector": f"count_over_time({SEL} [5m])"}, "must be a stream selector"),
    ]
    for tool, args, message in bad:
        result = await call(tool, args, seen)
        assert result.is_error, (tool, args)
        assert message in result.content[0].text, (tool, result.content[0].text)
    assert seen == []


async def test_loki_errors_surface() -> None:
    result = await call("query", {"query": f"boom(count_over_time({SEL} [5m]))"})
    assert result.is_error and "parse error" in result.content[0].text

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    http = httpx2.AsyncClient(base_url="http://loki", transport=httpx2.MockTransport(refuse))
    server = create_server(SETTINGS, LokiClient(SETTINGS, http))
    async with Client(server) as client:
        result = await client.call_tool("query", {"query": f"count_over_time({SEL} [5m])"})
    assert result.is_error and "unreachable" in result.content[0].text


async def test_labels_are_scoped_to_the_allowlist() -> None:
    seen: list[httpx2.Request] = []
    labels = (await call("list_labels", {}, seen)).structured_content
    assert labels["labels"] == ["app", "level"] and labels["truncated"] is True
    assert seen[0].url.params["query"] == '{namespace="prod"}'
    values = (
        await call("label_values", {"label": "app", "selector": SEL}, seen)
    ).structured_content
    assert values["values"] == ["order-service", "payment-service"]
    assert seen[1].url.path == "/loki/api/v1/label/app/values"
    assert seen[1].url.params["query"] == SEL


def test_number() -> None:
    assert number("1790330533719") == 1790330533719
    assert isinstance(number("3"), int)
    assert number("0.123456789") == 0.123457
    assert number("NaN") is None and number("x") is None


def test_request_headers_for_hosted_loki() -> None:
    assert request_headers(ServerSettings()) == {"Accept": "application/json"}
    token = "t-" + "x" * 8  # built at runtime: no secret-looking literal
    headers = request_headers(ServerSettings(loki_bearer_token=token, loki_org_id="tenant-a"))
    assert headers["Authorization"] == f"Bearer {token}"
    assert headers["X-Scope-OrgID"] == "tenant-a"
