"""Real log shipping, live (PR-016). Requires `make infra-up mcp-up k8s-up logging-up`.

Run with: make test-logging (or make test-integration). Skipped when Fluent Bit hasn't
shipped anything yet (no logs-k8s-* index).
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aiops.agents.deps import build_deps
from aiops.agents.log_agent import LogAgent
from aiops.core.config import load_settings
from aiops.core.models import AgentStatus, AgentTask, IncidentContext, TimeRange
from aiops.evals.replay import echo_responder
from aiops.llm.fake import FakeLLMProvider
from aiops.seed.k8s_logs import K8S_ILM_POLICY, K8S_TEMPLATE_NAME, ensure_k8s_logging

pytestmark = pytest.mark.integration
ES_URL = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")
CONFIG = Path(__file__).resolve().parents[3] / "config"
FRESH_WITHIN_S = 30.0
APP_FIELDS = (
    "@timestamp",
    "level",
    "message",
    "service",
    "environment",
    "version",
    "host",
    "logger",
)


def esql(query: str) -> list[list[Any]]:
    response = httpx2.post(f"{ES_URL}/_query", json={"query": query}, timeout=30)
    response.raise_for_status()
    values: list[list[Any]] = response.json()["values"]
    return values


@pytest.fixture(scope="module", autouse=True)
def shipped() -> None:
    try:
        response = httpx2.get(f"{ES_URL}/_cat/indices/logs-k8s-*", params={"format": "json"})
    except httpx2.HTTPError as exc:
        pytest.skip(f"Elasticsearch not reachable: {exc}")
    if response.status_code == 404 or not response.json():
        pytest.skip("no logs-k8s-* index yet: run `make k8s-up logging-up`")


def test_setup_is_idempotent_and_beats_the_builtin_logs_template() -> None:
    ensure_k8s_logging(ES_URL)
    ensure_k8s_logging(ES_URL)
    template = httpx2.get(f"{ES_URL}/_index_template/{K8S_TEMPLATE_NAME}").json()
    body = template["index_templates"][0]["index_template"]
    assert body["index_patterns"] == ["logs-k8s-*"] and body["priority"] > 100
    policy = httpx2.get(f"{ES_URL}/_ilm/policy/{K8S_ILM_POLICY}").json()
    assert policy[K8S_ILM_POLICY]["policy"]["phases"]["delete"]["min_age"] == "2d"
    # Plain daily indices managed by our ILM policy (not the built-in logs data stream).
    explain = httpx2.get(f"{ES_URL}/logs-k8s-*/_ilm/explain").json()["indices"]
    assert explain and all(i["policy"] == K8S_ILM_POLICY for i in explain.values())
    assert all(not name.startswith(".ds-") for name in explain)


def test_fresh_cluster_logs_reach_elasticsearch_within_30s() -> None:
    """Traffic of the last minute is searchable, with the app's fields lifted to top level."""
    deadline = time.monotonic() + FRESH_WITHIN_S
    rows: list[list[Any]] = []
    while time.monotonic() < deadline:
        since = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        rows = esql(
            f'FROM logs-k8s-* | WHERE @timestamp >= TO_DATETIME("{since}") '
            "AND service IS NOT NULL | STATS n = COUNT(*), newest = MAX(@timestamp) BY service"
        )
        if len(rows) >= 4:
            break
        time.sleep(2)
    services = {row[2] for row in rows}
    assert {"payment-service", "order-service", "user-service", "inventory-service"} <= services
    newest = max(datetime.fromisoformat(row[1].replace("Z", "+00:00")) for row in rows)
    assert datetime.now(UTC) - newest < timedelta(seconds=FRESH_WITHIN_S)

    response = httpx2.post(
        f"{ES_URL}/logs-k8s-*/_search",
        json={
            "size": 1,
            "sort": [{"@timestamp": "desc"}],
            "query": {"term": {"service": "payment-service"}},
        },
        timeout=30,
    )
    doc = response.json()["hits"]["hits"][0]["_source"]
    assert all(field in doc for field in APP_FIELDS), sorted(doc)
    assert "log" not in doc  # the JSON line was lifted, not kept as a string
    k8s = doc["kubernetes"]
    assert k8s["namespace_name"] == "prod" and k8s["container_name"] == "app"
    assert k8s["labels"]["app"] == "payment-service" and k8s["pod_name"].startswith("payment-")


def test_only_the_prod_namespace_is_shipped() -> None:
    rows = esql("FROM logs-k8s-* | STATS n = COUNT(*) BY ns = kubernetes.namespace_name")
    assert {row[1] for row in rows} == {"prod"}


def test_log_agent_runs_on_real_logs_with_config_only() -> None:
    """AIOPS_ENV=local-k8s: the unchanged Log agent queries logs-k8s-*, filtered by service."""
    settings = load_settings("local-k8s", CONFIG)
    deps = build_deps(settings, llm=FakeLLMProvider(responder=echo_responder("no_signal")))
    context = IncidentContext(
        question="Is anything wrong with payment-service in production?",
        service="payment-service",
        environment="production",
        time_range=TimeRange.last("15m", now=datetime.now(UTC)),
    )
    result = asyncio.run(
        LogAgent(deps).run(AgentTask(agent="logs", objective=context.question, context=context))
    )
    assert result.status is not AgentStatus.FAILED, result.summary
    queries = [c.arguments["query"] for c in result.tool_calls]
    assert queries and all(
        q.startswith('FROM logs-k8s-* | WHERE kubernetes.labels.app == "payment-service"')
        for q in queries
    )
    volume = result.evidence[0].summary
    assert not volume.startswith("0 errors in 0 lines"), volume
