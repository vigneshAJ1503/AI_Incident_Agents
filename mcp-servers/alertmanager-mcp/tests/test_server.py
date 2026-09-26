from __future__ import annotations

from typing import Any

import httpx2
from mcp import Client

from alertmanager_mcp.am import AlertmanagerClient
from alertmanager_mcp.config import ServerSettings
from alertmanager_mcp.prom import PrometheusClient
from alertmanager_mcp.server import HISTORY_NOTE, PROMETHEUS_HISTORY_NOTE, create_server

SETTINGS = ServerSettings(max_results=2, max_filters=3, max_time_range_hours=24)


def alert(
    name: str,
    service: str,
    severity: str,
    starts: str,
    fingerprint: str,
    *,
    state: str = "active",
    silenced_by: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "labels": {
            "alertname": name,
            "service": service,
            "severity": severity,
            "namespace": "prod",
        },
        "annotations": {
            "summary": f"{service}: {name}",
            "description": "details",
            "runbook_url": f"knowledge-base/runbooks/{name.lower()}.md",
        },
        "startsAt": starts,
        "endsAt": "2026-09-26T18:00:00.000Z",
        "updatedAt": "2026-09-25T18:00:00.000Z",
        "fingerprint": fingerprint,
        "receivers": [{"name": "null"}],
        "status": {"state": state, "silencedBy": silenced_by or [], "inhibitedBy": []},
        "generatorURL": f"aiops-seed://S1/{name}",
    }


ALERTS = [
    alert("HighLatencyP95", "payment-service", "warning", "2026-09-25T10:16:00.000Z", "aaa1"),
    alert("HighErrorRate", "payment-service", "critical", "2026-09-25T10:12:00.000Z", "bbb2"),
    alert(
        "DatabaseConnectionPoolExhausted",
        "payment-service",
        "critical",
        "2026-09-25T10:11:00.000Z",
        "ccc3",
    ),
    alert(
        "HighLatencyP95",
        "inventory-service",
        "warning",
        "2026-09-25T09:00:00.000Z",
        "ddd4",
        state="suppressed",
        silenced_by=["sil-1"],
    ),
]

SILENCES = [
    {
        "id": "sil-1",
        "status": {"state": "active"},
        "matchers": [
            {"name": "service", "value": "inventory-service", "isEqual": True, "isRegex": False},
            {"name": "alertname", "value": "HighLatency.*", "isEqual": True, "isRegex": True},
        ],
        "startsAt": "2026-09-25T08:00:00.000Z",
        "endsAt": "2026-09-25T20:00:00.000Z",
        "createdBy": "oncall",
        "comment": "known slow query, OPS-31",
    },
    {
        "id": "sil-2",
        "status": {"state": "expired"},
        "matchers": [{"name": "alertname", "value": "RedisDown", "isEqual": True}],
        "startsAt": "2026-09-24T08:00:00.000Z",
        "endsAt": "2026-09-24T09:00:00.000Z",
        "createdBy": "oncall",
        "comment": "maintenance",
    },
    {
        "id": "sil-3",
        "status": {"state": "active"},
        "matchers": [{"name": "service", "value": "payment-service", "isEqual": False}],
        "startsAt": "2026-09-25T08:00:00.000Z",
        "endsAt": "2026-09-25T20:00:00.000Z",
        "createdBy": "oncall",
        "comment": "everything but payments",
    },
]


def matches(a: dict[str, Any], filters: list[str]) -> bool:
    for f in filters:
        name, value = f.split("=", 1)
        if a["labels"].get(name) != value.strip('"'):
            return False
    return True


def fake_am(seen: list[httpx2.Request]) -> httpx2.AsyncClient:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        assert request.method == "GET"  # read-only
        params = request.url.params
        filters = params.get_list("filter")
        active = params.get("active") == "true"
        silenced = params.get("silenced") == "true"
        selected = [
            a
            for a in ALERTS
            if matches(a, filters)
            and (
                (a["status"]["state"] == "active" and active)
                or (a["status"]["silencedBy"] and silenced)
            )
        ]
        if request.url.path == "/api/v2/alerts":
            return httpx2.Response(200, json=selected)
        if request.url.path == "/api/v2/alerts/groups":
            groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for a in selected:
                key = (a["labels"]["service"], a["labels"]["alertname"])
                groups.setdefault(key, []).append(a)
            return httpx2.Response(
                200,
                json=[
                    {
                        "labels": {"service": s, "alertname": n},
                        "receiver": {"name": "null"},
                        "alerts": alerts,
                    }
                    for (s, n), alerts in groups.items()
                ],
            )
        if request.url.path == "/api/v2/silences":
            return httpx2.Response(200, json=SILENCES)
        return httpx2.Response(404, text="not found")

    return httpx2.AsyncClient(base_url="http://am", transport=httpx2.MockTransport(handle))


async def call(
    tool: str,
    args: dict[str, Any],
    seen: list[httpx2.Request] | None = None,
    settings: ServerSettings = SETTINGS,
) -> Any:
    seen = seen if seen is not None else []
    server = create_server(settings, AlertmanagerClient(settings, fake_am(seen)))
    async with Client(server) as client:
        return await client.call_tool(tool, args)


async def test_tools_listed() -> None:
    server = create_server(SETTINGS, AlertmanagerClient(SETTINGS, fake_am([])))
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "list_alerts",
        "get_alert",
        "list_silences",
        "get_alert_groups",
        "alert_history",
    }
    assert "labels" in tools["list_alerts"].input_schema["properties"]


async def test_list_alerts_filters_sorts_and_caps() -> None:
    seen: list[httpx2.Request] = []
    result = await call(
        "list_alerts", {"service": "payment-service", "labels": {"namespace": "prod"}}, seen
    )
    data = result.structured_content
    params = seen[0].url.params
    assert params.get_list("filter") == ['namespace="prod"', 'service="payment-service"']
    assert (params["active"], params["silenced"], params["inhibited"]) == ("true", "false", "false")
    assert data["total"] == 3 and data["returned"] == 2 and data["truncated"] is True
    assert data["by_severity"] == {"critical": 2, "warning": 1}
    # critical first, then by start time
    assert [a["alertname"] for a in data["alerts"]] == [
        "DatabaseConnectionPoolExhausted",
        "HighErrorRate",
    ]
    first = data["alerts"][0]
    assert first["runbook_url"] == "knowledge-base/runbooks/databaseconnectionpoolexhausted.md"
    assert first["starts_at"] == "2026-09-25T10:11:00.000Z"
    assert "ends_at" not in first  # an expiry time, not a resolution time


async def test_list_alerts_suppressed_state() -> None:
    seen: list[httpx2.Request] = []
    result = await call("list_alerts", {"state": "suppressed"}, seen)
    data = result.structured_content
    assert [a["fingerprint"] for a in data["alerts"]] == ["ddd4"]
    assert data["alerts"][0]["silenced_by"] == ["sil-1"]
    assert seen[0].url.params["active"] == "false"


async def test_label_filter_guardrails_reject_without_calls() -> None:
    seen: list[httpx2.Request] = []
    bad_calls: list[tuple[str, dict[str, Any], str]] = [
        ("list_alerts", {"labels": {"service=~": ".*"}}, "invalid label name"),
        ("list_alerts", {"labels": {"a": "1", "b": "2", "c": "3", "d": "4"}}, "at most 3"),
        ("list_alerts", {"labels": {"service": ""}}, "non-empty"),
        ("list_alerts", {"labels": {"service": "x\ny"}}, "control characters"),
        ("list_alerts", {"service": "a", "labels": {"service": "b"}}, "conflicting"),
        ("list_alerts", {"limit": 0}, "limit must be >= 1"),
        ("get_alert", {"fingerprint": "not-hex!"}, "hex string"),
        ("alert_history", {"start": "yesterday", "end": "2026-09-25T10:30:00Z"}, "ISO-8601"),
        (
            "alert_history",
            {"start": "2026-09-20T00:00:00Z", "end": "2026-09-25T10:30:00Z"},
            "exceeds the maximum of 24 hours",
        ),
        ("list_silences", {"labels": {"bad name": "x"}}, "invalid label name"),
    ]
    for tool, args, message in bad_calls:
        result = await call(tool, args, seen)
        assert result.is_error, (tool, args)
        assert message in result.content[0].text, (tool, result.content[0].text)
    assert seen == []


async def test_quotes_in_values_are_escaped() -> None:
    seen: list[httpx2.Request] = []
    await call("list_alerts", {"service": 'pay"ment'}, seen)
    assert seen[0].url.params.get_list("filter") == ['service="pay\\"ment"']


async def test_get_alert() -> None:
    result = await call("get_alert", {"fingerprint": "BBB2"})
    assert result.structured_content["alert"]["alertname"] == "HighErrorRate"
    missing = await call("get_alert", {"fingerprint": "ffff"})
    assert missing.is_error and "may have resolved" in missing.content[0].text


async def test_list_silences_applicability_and_state() -> None:
    result = await call("list_silences", {"service": "inventory-service"})
    ids = [s["id"] for s in result.structured_content["silences"]]
    assert ids == ["sil-1", "sil-3"]  # sil-3: service != payment-service also matches
    assert result.structured_content["silences"][0]["matchers"] == [
        'service="inventory-service"',
        'alertname=~"HighLatency.*"',
    ]
    payments = await call("list_silences", {"service": "payment-service"})
    assert payments.structured_content["silences"] == []
    everything = await call("list_silences", {"state": "all", "limit": 10})
    assert everything.structured_content["total"] == 3


async def test_alert_groups_budget() -> None:
    seen: list[httpx2.Request] = []
    result = await call("get_alert_groups", {"service": "payment-service"}, seen)
    data = result.structured_content
    assert seen[0].url.path == "/api/v2/alerts/groups"
    assert data["group_count"] == 3 and data["total_alerts"] == 3 and data["truncated"] is True
    assert data["groups"][0]["labels"]["alertname"] == "DatabaseConnectionPoolExhausted"
    assert sum(len(g["alerts"]) for g in data["groups"]) == 2
    assert all(g["alert_count"] == 1 for g in data["groups"])


async def test_alert_history_is_honest_about_coverage() -> None:
    settings = ServerSettings(max_results=10, max_time_range_hours=24)
    result = await call(
        "alert_history",
        {"start": "2026-09-25T10:00:00Z", "end": "2026-09-25T10:15:00Z"},
        settings=settings,
    )
    data = result.structured_content
    assert data["complete"] is False and data["note"] == HISTORY_NOTE
    assert data["sources"] == ["alertmanager"]
    assert "ALERTS" in data["note"]
    # started before the end of the range, oldest first (silenced ones included)
    assert [a["fingerprint"] for a in data["alerts"]] == ["ddd4", "ccc3", "bbb2"]


async def test_alertmanager_errors_surface() -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, text="boom")

    http = httpx2.AsyncClient(base_url="http://am", transport=httpx2.MockTransport(broken))
    server = create_server(SETTINGS, AlertmanagerClient(SETTINGS, http))
    async with Client(server) as client:
        result = await client.call_tool("list_alerts", {})
    assert result.is_error and "Alertmanager error 500: boom" in result.content[0].text

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    http = httpx2.AsyncClient(base_url="http://am", transport=httpx2.MockTransport(refuse))
    server = create_server(SETTINGS, AlertmanagerClient(SETTINGS, http))
    async with Client(server) as client:
        result = await client.call_tool("list_silences", {})
    assert result.is_error and "unreachable" in result.content[0].text


# --------------------------------------------------------------------------- Prometheus ALERTS

T0 = 1790330400  # 2026-09-25T10:00:00Z


def fake_prom(seen: list[httpx2.Request], *, fail: bool = False) -> httpx2.AsyncClient:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        assert request.method == "GET"  # read-only
        assert request.url.path == "/api/v1/query_range"
        if fail:
            return httpx2.Response(503, text="unavailable")
        labels = {
            "__name__": "ALERTS",
            "namespace": "prod",
            "service": "payment-service",
            "alertstate": "firing",
        }
        return httpx2.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {  # resolved: fired 10:02-10:05 (samples every 30 s)
                            "metric": {
                                **labels,
                                "alertname": "HighLatencyP95",
                                "severity": "warning",
                            },
                            "values": [[T0 + 120 + 30 * i, "1"] for i in range(7)],
                        },
                        {  # same labels as the Alertmanager alert ccc3: still firing at the end
                            "metric": {
                                **labels,
                                "alertname": "DatabaseConnectionPoolExhausted",
                                "severity": "critical",
                            },
                            "values": [[T0 + 600 + 30 * i, "1"] for i in range(11)],
                        },
                    ],
                },
            },
        )

    return httpx2.AsyncClient(base_url="http://prom", transport=httpx2.MockTransport(handle))


async def history_with_prometheus(fail: bool = False) -> tuple[Any, list[httpx2.Request]]:
    settings = ServerSettings(max_results=10, max_time_range_hours=24, prometheus_url="http://prom")
    prom_seen: list[httpx2.Request] = []
    server = create_server(
        settings,
        AlertmanagerClient(settings, fake_am([])),
        PrometheusClient(settings, fake_prom(prom_seen, fail=fail)),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "alert_history",
            {
                "start": "2026-09-25T10:00:00Z",
                "end": "2026-09-25T10:15:00Z",
                "service": "payment-service",
                "labels": {"namespace": "prod"},
            },
        )
    return result.structured_content, prom_seen


async def test_alert_history_from_prometheus_alerts() -> None:
    data, seen = await history_with_prometheus()
    params = seen[0].url.params
    assert params["query"] == (
        'ALERTS{alertstate="firing",namespace="prod",service="payment-service"}'
    )
    assert params["step"] == "30"
    assert data["complete"] is True and data["note"] == PROMETHEUS_HISTORY_NOTE
    assert data["sources"] == ["prometheus", "alertmanager"]
    by_name = {(a["alertname"], a["source"]): a for a in data["alerts"]}
    resolved = by_name[("HighLatencyP95", "prometheus")]
    assert resolved["state"] == "resolved"
    assert resolved["firing_since"] == "2026-09-25T10:02:00Z"
    assert resolved["resolved_at"] == "2026-09-25T10:05:30Z"
    assert resolved["started_before_range"] is False
    firing = by_name[("DatabaseConnectionPoolExhausted", "prometheus")]
    assert firing["state"] == "firing" and firing["resolved_at"] is None
    # annotations joined from Alertmanager by identical labels
    assert firing["fingerprint"] == "ccc3" and firing["runbook_url"]
    # Alertmanager-only alerts (e.g. seeded, no ALERTS series) are kept
    assert ("HighErrorRate", "alertmanager") in by_name
    assert ("DatabaseConnectionPoolExhausted", "alertmanager") not in by_name
    assert data["alerts"][0]["alertname"] == "HighLatencyP95"  # oldest first


async def test_alert_history_falls_back_when_prometheus_fails() -> None:
    data, _ = await history_with_prometheus(fail=True)
    assert data["complete"] is False and data["sources"] == ["alertmanager"]
    assert "Prometheus could not be queried" in data["note"]
