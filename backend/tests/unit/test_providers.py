"""Provider adapters (ADR-0012): registry, matrix integration, prompt fragments, contracts.

The strongest contract test is the replay suite: every recorded fixture replays through
the adapters with byte-identical tool calls (tests/unit/test_*_agent.py).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from aiops.core.config import ConfigError, load_settings
from aiops.core.profiles import PROVIDERS, check_settings
from aiops.core.prompts import PromptLoader
from aiops.providers import PROVIDER_REGISTRY, Provider, ToolRequest
from aiops.providers.alerts.alertmanager import AlertmanagerAlerts
from aiops.providers.code.git import GitCode
from aiops.providers.logs import LogScope, LogsProvider, LogWindow
from aiops.providers.logs.elasticsearch import ElasticsearchLogs
from aiops.providers.metrics import SLIS, MetricScope, MetricsProvider, MetricWindow
from aiops.providers.metrics.prometheus import PrometheusMetrics, parse_step, regex_alternation
from aiops.providers.registry import ProviderRegistry
from aiops.providers.tickets.jira import JiraTickets, MockTickets

CONFIG = Path(__file__).resolve().parents[3] / "config"


def test_builtin_providers_are_discovered() -> None:
    assert PROVIDER_REGISTRY.find("logs", "elasticsearch") is ElasticsearchLogs
    assert "logs" in PROVIDER_REGISTRY.capabilities()
    # _skeleton.py is never imported, so never registered.
    assert PROVIDER_REGISTRY.find("logs", "skeleton") is None


def test_matrix_marks_registered_providers_implemented() -> None:
    for cls in PROVIDER_REGISTRY.all():
        spec = PROVIDERS[cls.capability][cls.name]
        assert spec.status == "implemented" and spec.mcp == cls.mcp
        assert spec.agent_tools == cls.agent_tools
    assert PROVIDERS["logs"]["loki"].status == "implemented"  # PR-P4a
    assert PROVIDERS["logs"]["splunk"].status == "planned"


def test_registered_providers_have_prompt_fragments() -> None:
    loader = PromptLoader(CONFIG / "prompts")
    for cls in PROVIDER_REGISTRY.all():
        if cls.prompt_fragment:
            fragment = loader.load(cls.prompt_fragment)
            assert fragment.ref.startswith(f"providers/{cls.capability}/")


def test_registry_errors_are_readable() -> None:
    registry = ProviderRegistry()
    registry.register(ElasticsearchLogs)
    with pytest.raises(
        ConfigError, match=r"no provider adapter 'splunk'.*implemented: elasticsearch"
    ):
        registry.get("logs", "splunk")

    class Other(ElasticsearchLogs):
        pass

    with pytest.raises(ValueError, match="already registered"):
        registry.register(Other)


def test_for_settings_builds_the_profile_provider() -> None:
    settings = load_settings("local", CONFIG)
    logs = PROVIDER_REGISTRY.for_settings(settings, "logs", LogsProvider)
    assert isinstance(logs, ElasticsearchLogs)
    assert logs.settings == settings.capability("logs").settings

    class NotLogs(Provider):
        capability: ClassVar[str] = "logs"
        name: ClassVar[str] = "elasticsearch"

    with pytest.raises(ConfigError, match="is not a NotLogs"):
        PROVIDER_REGISTRY.for_settings(settings, "logs", NotLogs)


def test_validate_uses_the_registry() -> None:
    settings = load_settings("local", CONFIG)
    assert not [i for i in check_settings(settings).errors if "capability logs" in i.message]


# -- logs/elasticsearch contract ------------------------------------------------------------

START = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
WINDOW = LogWindow(START, START + timedelta(minutes=30), timedelta(hours=24))


def scope(**overrides: object) -> LogScope:
    values: dict[str, object] = {
        "index": "logs-payment-*",
        "fields": ElasticsearchLogs().fields() | {"version": "version"},
        "error_levels": ["ERROR"],
        "pattern_levels": ["ERROR", "WARN"],
        "baseline": timedelta(hours=24),
        "startup_pattern": "Starting*",
        "link_template": "http://kibana/app/discover#/?from={start}&to={end}&q={kql}",
    }
    values.update(overrides)
    return LogScope(**values)  # type: ignore[arg-type]


def test_elasticsearch_golden_queries() -> None:
    es = ElasticsearchLogs()
    s = scope()
    volume = es.volume_by_level(s, WINDOW)
    assert volume == ToolRequest(
        "execute_esql",
        {
            "query": 'FROM logs-payment-* | EVAL window = CASE(@timestamp >= TO_DATETIME("2026-09-25T10:00:00Z"), "current", "baseline") | STATS count = COUNT(*) BY window, level = level',
            "start": "2026-09-24T10:00:00Z",
            "end": "2026-09-25T10:30:00Z",
        },
    )
    patterns = es.message_patterns(s, WINDOW, 10).arguments["query"]
    assert 'WHERE level IN ("ERROR", "WARN")' in patterns and "msg = message.keyword" in patterns
    first = es.first_occurrences(s, WINDOW, 'Pool "x" 50*', 5)
    assert 'message LIKE "Pool \\"x\\" 50\\\\**"' in first.arguments["query"]
    assert dict(first.columns) == {
        "@timestamp": "timestamp",
        "trace_id": "trace_id",
        "version": "version",
    }


def test_elasticsearch_normalizes_columns_and_links() -> None:
    es = ElasticsearchLogs()
    s = scope(service_value="payment-service")
    request = es.first_occurrences(s, WINDOW, "Database timeout", 5)
    table = es.table(request, {"columns": ["@timestamp", "trace_id"], "rows": [["t1", "abc"]]})
    assert table is not None and table.columns == ["timestamp", "trace_id"]
    assert es.table(request, "not tabular") is None
    link = es.ui_link(s, WINDOW, levels=["ERROR", "WARN"])
    assert (
        link is not None
        and "q=service%3A%22payment-service%22%20and%20%28level%3A%28ERROR%20or%20WARN%29%29"
        in link
    )
    assert "MUST filter" in es.scope_note(s) and es.scope_note(scope()) == ""
    assert es.phrase("Short <NUM>") is None


# -- tickets/jira, alerts/alertmanager, code/git contracts ------------------------------------


def test_every_capability_has_an_adapter() -> None:
    for capability in ("logs", "metrics", "tickets", "alerts", "code", "k8s", "knowledge"):
        assert PROVIDER_REGISTRY.names(capability), capability


def test_jira_and_mock_share_the_contract() -> None:
    for cls in (JiraTickets, MockTickets):
        request = cls().scope_search("OPS", ["payments"], ["payment-service"], START, 20)
        assert request.tool == "jira_search" and request.arguments["limit"] == 20
        assert request.arguments["jql"] == (
            'project = "OPS" AND (component in ("payments") OR labels in ("payment-service")) '
            'AND (statusCategory != Done OR resolved >= "2026-09-25") ORDER BY updated DESC'
        )
    words = JiraTickets().keyword_search("OPS", ["oom*", "memory", "oom*"], START, 5)
    assert '(text ~ "oom*" OR text ~ "memory") AND' in words.arguments["jql"]
    assert JiraTickets().tickets({"issues": [{"key": "OPS-1", "summary": "x"}]})[0].key == "OPS-1"


def test_alertmanager_requests_links_and_history() -> None:
    am = AlertmanagerAlerts()
    labels = {"service": "payment-service"}
    assert am.alerts_for(labels, 50) == ToolRequest(
        "list_alerts", {"labels": labels, "state": "all", "limit": 50}
    )
    assert am.silences_for(labels, 20).arguments["state"] == "active"
    assert am.ui_link("http://am/#/alerts?filter={filter}", labels) == (
        "http://am/#/alerts?filter=%7Bservice%3D%22payment-service%22%7D"
    )
    assert am.ui_link(None, labels) is None
    assert not am.has_history and am.history_note and "no history" in am.history_note


def test_git_requests() -> None:
    git = GitCode()
    assert git.releases_of("shop", 30) == ToolRequest(
        "list_releases", {"repo": "shop", "limit": 30}
    )
    commits = git.commits_touching("shop", START, START + timedelta(hours=1), ("payment/",), 30)
    assert commits.arguments == {
        "repo": "shop",
        "since": "2026-09-25T10:00:00Z",
        "until": "2026-09-25T11:00:00Z",
        "paths": ["payment/"],
        "limit": 30,
    }
    assert git.diff_of("shop", "abc", ("payment/",)).tool == "get_diff"
    assert git.commits(None) == [] and git.diff_files({"files": [{"path": "a"}]}) == [{"path": "a"}]


# -- metrics/prometheus contract -----------------------------------------------------------

M_WINDOW = MetricWindow(START, START + timedelta(minutes=30))


def m_scope(workloads: list[str] | None = None) -> MetricScope:
    return MetricScope(
        services=["payment-service", "user.svc"],
        workloads=["payment-service"] if workloads is None else workloads,
        filters={"namespace": "prod"},
        service="payment-service",
        namespace="prod",
    )


def test_metrics_matrix_uses_the_registry() -> None:
    assert PROVIDER_REGISTRY.find("metrics", "prometheus") is PrometheusMetrics
    assert PROVIDERS["metrics"]["prometheus"].status == "implemented"
    assert PROVIDERS["metrics"]["datadog"].status == "planned"
    assert PROVIDER_REGISTRY.find("metrics", "datadog") is None  # _skeleton.py is inert
    settings = load_settings("local", CONFIG)
    assert not [i for i in check_settings(settings).errors if "capability metrics" in i.message]
    prom = PROVIDER_REGISTRY.for_settings(settings, "metrics", MetricsProvider)
    assert isinstance(prom, PrometheusMetrics) and prom.label("k8s_app") == "label_app"


def test_prometheus_golden_queries_from_settings() -> None:
    prom = PrometheusMetrics(
        {
            "labels": {"service": "app"},
            "metrics": {"requests": "requests_count"},
            "rate_window": "5m",
        }
    )
    requests = {sli.key: prom.series_request(sli, m_scope(), M_WINDOW) for sli in SLIS}
    assert list(requests) == [
        "rps",
        "error_rate",
        "latency_p95",
        "latency_p99",
        "db_pool_utilization",
        "db_pool_pending",
        "cache_up",
        "memory_rss",
        "restarts",
        "oom_killed",
    ]
    rps = requests["rps"]
    assert rps is not None
    assert rps.request == ToolRequest(
        "query_range",
        {
            "query": 'sum by (app) (rate(requests_count{app=~"payment-service|user\\\\.svc",namespace="prod"}[5m]))',
            "start": "2026-09-25T10:00:00Z",
            "end": "2026-09-25T10:30:00Z",
            "step": "30s",
        },
    )
    assert rps.query == rps.request.arguments["query"] and rps.group == "app"
    errors = requests["error_rate"]
    assert errors is not None and 'status=~"5.."' in errors.query and " * 0) / " in errors.query
    restarts, oom = requests["restarts"], requests["oom_killed"]
    assert restarts is not None and restarts.group == "label_app"
    assert oom is not None and 'reason="OOMKilled"' in oom.query
    assert regex_alternation(["a+b"]) == '"a\\\\+b"'
    # no workloads (no k8s identifiers): pod-level SLIs are skipped, not failed
    no_k8s = [s for s in SLIS if prom.series_request(s, m_scope([]), M_WINDOW) is not None]
    assert len(no_k8s) == 8


def test_prometheus_step_series_and_links() -> None:
    assert parse_step("1m") == 60 and parse_step("45") == 45 and parse_step("x") == 30
    day = MetricWindow(START, START + timedelta(days=1))
    assert PrometheusMetrics().step_seconds(day) == 87  # capped at 1000 points
    prom = PrometheusMetrics(
        {
            "ui_link_template": "http://g/d/x?var-service={service}&viewPanel={panel}&from={from_ms}",
            "explore_link_template": "http://p/query?g0.expr={query}&g0.range_input={range}",
            "panels": {"error_rate": 6},
        }
    )
    sli = {s.key: s for s in SLIS}
    errors = prom.series_request(sli["error_rate"], m_scope(), M_WINDOW)
    restarts = prom.series_request(sli["restarts"], m_scope(), M_WINDOW)
    assert errors is not None and restarts is not None
    assert prom.ui_link(m_scope(), errors, M_WINDOW) == (
        f"http://g/d/x?var-service=payment-service&viewPanel=6&from={int(START.timestamp() * 1000)}"
    )
    # no panel for the SLI: the exact query instead
    link = prom.ui_link(m_scope(), restarts, M_WINDOW)
    assert link == prom.query_link(restarts, M_WINDOW)
    assert link is not None and link.startswith("http://p/query?g0.expr=max%20by")
    assert link.endswith("g0.range_input=30m")
    data = {
        "series": [
            {"labels": {"label_app": "a"}, "values": [[1, 0.5], [2, None], [3, "x"]]},
            {"labels": {"other": "b"}, "values": [[1, 1.0]]},
        ]
    }
    assert prom.series(restarts, data) == {"a": [(1.0, 0.5), (2.0, None), (3.0, None)]}
    assert prom.series(restarts, None) == {}
