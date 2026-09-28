"""``logs/loki`` adapter contract (PR-P4a): golden LogQL, normalization, links, matrix."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

from aiops.core.config import load_settings
from aiops.core.profiles import PROVIDERS
from aiops.providers import PROVIDER_REGISTRY
from aiops.providers.logs import LogScope, LogsProvider, LogWindow
from aiops.providers.logs.loki import LokiLogs, glob_regex, logql_string

CONFIG = Path(__file__).resolve().parents[3] / "config"
START = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)  # 1790330400000 ms
WINDOW = LogWindow(start=START, end=START + timedelta(minutes=10), baseline=timedelta(minutes=15))
SPLIT = (
    'label_format window=`{{ if ge (unixEpochMillis __timestamp__) "1790330400000" }}'
    "current{{ else }}baseline{{ end }}`"
)
LOKI = LokiLogs({"stream_labels": ["namespace", "app", "level"]})


def scope(service_value: str | None = "payment-service", **fields: str) -> LogScope:
    return LogScope(
        index='{namespace="prod"}',
        fields={**LOKI.fields(), "version": "version", **fields},
        error_levels=["ERROR"],
        pattern_levels=["ERROR", "WARN"],
        baseline=timedelta(minutes=15),
        startup_pattern="Starting*",
        link_template="http://g/explore?panes={panes}",
        service_value=service_value,
    )


def test_registered_and_implemented() -> None:
    assert PROVIDER_REGISTRY.find("logs", "loki") is LokiLogs
    spec = PROVIDERS["logs"]["loki"]
    assert spec.status == "implemented" and spec.mcp == "mcp-servers/loki-mcp"
    assert spec.agent_tools == ("query", "query_range") and spec.required == ("stream_labels",)


def test_local_loki_profile_selects_the_adapter() -> None:
    settings = load_settings("local-loki", CONFIG)
    logs = settings.capability("logs")
    assert logs.provider == "loki" and logs.mcp.url.endswith(":8110/mcp")
    provider = PROVIDER_REGISTRY.for_settings(settings, "logs", LogsProvider)
    assert isinstance(provider, LokiLogs)
    assert provider.fields()["service"] == "app"
    # Only the logs capability differs from local-k8s.
    base = load_settings("local-k8s", CONFIG)
    for name in ("metrics", "alerts", "k8s", "code", "tickets", "knowledge"):
        assert settings.capability(name) == base.capability(name), name


def test_volume_golden_query() -> None:
    request = LOKI.volume_by_level(scope(), WINDOW)
    assert request.tool == "query"
    assert request.arguments == {
        "query": (
            "label_replace(sum by (window, level) (count_over_time("
            f'{{namespace="prod", app="payment-service"}} | {SPLIT} [1500s])), '
            '"stat", "count", "", "")'
        ),
        "time": "2026-09-25T10:10:00Z",
    }


def test_patterns_golden_query() -> None:
    query = LOKI.message_patterns(scope(), WINDOW, 1000).arguments["query"]
    pipe = (
        '{namespace="prod", app="payment-service", level=~"^(?:ERROR|WARN)$"} '
        f'| json msg="message" | drop __error__, __error_details__ | {SPLIT}'
    )
    ts = f"{pipe} | label_format ts_ms=`{{{{ unixEpochMillis __timestamp__ }}}}` | unwrap ts_ms"
    assert query == (
        f"label_replace(topk(1000, sum by (window, level, msg) (count_over_time({pipe} [1500s]))), "
        '"stat", "count", "", "") or '
        f"label_replace(min by (window, level, msg) (min_over_time({ts} [1500s])), "
        '"stat", "first_seen", "", "") or '
        f"label_replace(max by (window, level, msg) (max_over_time({ts} [1500s])), "
        '"stat", "last_seen", "", "")'
    )


def test_versions_and_first_occurrences_golden_queries() -> None:
    query = LOKI.versions_and_startups(scope(), WINDOW).arguments["query"]
    assert 'json msg="message", version="version"' in query
    assert (
        'sum by (window, version) (count_over_time({namespace="prod", app="payment-service"}'
        in query
    )
    assert '| msg=~"^Starting.*$" [1500s]' in query and '"stat", "starts"' in query
    first = LOKI.first_occurrences(scope(), WINDOW, "Database connection (pool)", 5)
    assert first.tool == "query_range"
    assert first.arguments == {
        "query": (
            '{namespace="prod", app="payment-service"} |= "Database connection (pool)" '
            '| json msg="message", trace_id="trace_id", version="version" '
            '| drop __error__, __error_details__ | msg=~"^Database connection \\\\(pool\\\\).*"'
        ),
        "start": "2026-09-25T10:00:00Z",
        "end": "2026-09-25T10:10:00Z",
        "limit": 5,
        "direction": "forward",
    }


def test_json_level_and_service_are_label_filters() -> None:
    """A company whose level/service are JSON keys (no such stream labels)."""
    provider = LokiLogs({"stream_labels": ["namespace"]})
    s = scope(level="log.level", service="service.name")
    query = provider.message_patterns(s, WINDOW, 10).arguments["query"]
    assert query.startswith(
        'label_replace(topk(10, sum by (window, level, msg) (count_over_time({namespace="prod"} '
        '| json level="log.level", msg="message", svc="service.name" '
        '| drop __error__, __error_details__ | svc="payment-service" '
        '| level=~"^(?:ERROR|WARN)$" | label_format window='
    )
    # No shared-stream filter when the service filter is off.
    assert provider.selector(scope(None)) == '{namespace="prod"}'


def test_table_pivots_stats_into_neutral_rows() -> None:
    request = LOKI.message_patterns(scope(), WINDOW, 1000)
    data = {
        "series": [
            {
                "labels": {"level": "ERROR", "msg": "boom", "stat": "count", "window": "current"},
                "value": 8,
            },
            {
                "labels": {
                    "level": "ERROR",
                    "msg": "boom",
                    "stat": "first_seen",
                    "window": "current",
                },
                "value": 1790330533719,
            },
            {
                "labels": {
                    "level": "ERROR",
                    "msg": "boom",
                    "stat": "last_seen",
                    "window": "current",
                },
                "value": 1790330588473,
            },
            # beyond topk: first/last seen without a count -> dropped
            {
                "labels": {
                    "level": "WARN",
                    "msg": "rare",
                    "stat": "first_seen",
                    "window": "baseline",
                },
                "value": 1,
            },
        ]
    }
    table = LOKI.table(request, data)
    assert table is not None
    assert table.columns == ["count", "first_seen", "last_seen", "level", "msg", "window"]
    assert table.rows == [
        [8, "2026-09-25T10:02:13.719Z", "2026-09-25T10:03:08.473Z", "ERROR", "boom", "current"]
    ]
    versions = LOKI.table(
        LOKI.versions_and_startups(scope(), WINDOW),
        {
            "series": [
                {
                    "labels": {"stat": "count", "version": "v1.8.1", "window": "baseline"},
                    "value": 10,
                },
                {"labels": {"stat": "count", "version": "", "window": "current"}, "value": 2},
            ]
        },
    )
    assert versions is not None
    assert versions.rows == [[10, "v1.8.1", "baseline"], [2, None, "current"]]  # "" = missing


def test_table_of_log_lines() -> None:
    request = LOKI.first_occurrences(scope(), WINDOW, "Database connection", 5)
    data = {
        "lines": [
            {
                "timestamp": "2026-09-25T10:02:05.140Z",
                "labels": {"app": "payment-service", "trace_id": "abc", "version": "v1.8.2"},
                "line": "{}",
            }
        ]
    }
    table = LOKI.table(request, data)
    assert table is not None
    assert table.columns == ["timestamp", "trace_id", "version"]
    assert table.rows == [["2026-09-25T10:02:05.140Z", "abc", "v1.8.2"]]
    assert LOKI.table(request, None) is None


def test_grafana_explore_links() -> None:
    link = LOKI.ui_link(scope(), WINDOW, levels=["ERROR", "WARN"])
    assert link is not None and link.startswith("http://g/explore?panes=")
    panes = json.loads(unquote(link.split("panes=", 1)[1]))
    pane = panes["a"]
    assert pane["range"] == {"from": "1790330400000", "to": "1790331000000"}
    assert pane["queries"][0]["expr"] == (
        '{namespace="prod", app="payment-service", level=~"^(?:ERROR|WARN)$"}'
    )
    assert pane["queries"][0]["datasource"] == {"type": "loki", "uid": "loki"}
    phrase = LOKI.ui_link(scope(), WINDOW, phrase="Database connection")
    assert phrase is not None and "Database%20connection" in phrase
    no_template = LogScope(**{**scope().__dict__, "link_template": None})
    assert LOKI.ui_link(no_template, WINDOW) is None


def test_scope_note_and_helpers() -> None:
    note = LOKI.scope_note(scope())
    assert "`app`" in note and "MUST select this service" in note
    assert '{namespace="prod", app="payment-service"}' in note
    assert glob_regex("Starting*") == "^Starting.*$" and glob_regex("a?.b") == "^a.\\.b$"
    assert logql_string('say "hi"\\') == '"say \\"hi\\"\\\\"'
