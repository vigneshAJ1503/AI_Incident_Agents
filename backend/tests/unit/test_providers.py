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
    assert PROVIDERS["logs"]["loki"].status == "planned"


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
        ConfigError, match=r"no provider adapter 'loki'.*implemented: elasticsearch"
    ):
        registry.get("logs", "loki")

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
    for capability in ("logs", "tickets", "alerts", "code", "k8s", "knowledge"):
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
