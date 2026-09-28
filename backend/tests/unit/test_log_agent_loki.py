"""THE portability proof (PR-P4a): the unchanged Log agent on Grafana Loki.

Same cluster, same Fluent Bit, same Log agent, same scenario ground truth as the
Elasticsearch runs (test_log_agent_k8s.py); only the profile differs
(AIOPS_PROFILE=local-loki: capabilities.logs.provider = loki, loki-mcp, a stream selector
in the catalog). Fixtures were recorded LIVE from Loki while `aiops fault run` held the
cluster lock (tests/fixtures/record_logs_loki.py); each has a meta.json with its window.
Zero tokens: the fake LLM only submits; results are scored on the deterministic phase.
test_portability_proof.py enforces that this PR changed no agent/orchestrator code.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from aiops.agents.deps import build_deps
from aiops.agents.log_agent import LogAgent
from aiops.core.config import load_settings
from aiops.core.models import AgentResult, AgentStatus, AgentTask
from aiops.evals.replay import echo_responder
from aiops.evals.scoring import score_agent
from aiops.llm.fake import FakeLLMProvider
from tests.fixtures.record_logs_k8s import task_for_window
from tests.fixtures.scenario_context import SCENARIOS

TESTS = Path(__file__).resolve().parents[1]
FIXTURES = TESTS / "fixtures" / "logs-loki"
ES_FIXTURES = TESTS / "fixtures" / "logs-k8s"
CONFIG = Path(__file__).resolve().parents[3] / "config"
ENV = "local-loki"
RECORDED = sorted(p.parent.name for p in FIXTURES.glob("*/meta.json"))
#: What the incident must show on EITHER backend (the task's acceptance list).
S1_SIGNALS = {"db_timeout_errors_up", "error_rate_up", "new_error_pattern", "deployment_detected"}


def meta(scenario: str, root: Path = FIXTURES) -> dict[str, str]:
    data: dict[str, str] = json.loads((root / scenario / "meta.json").read_text())
    return data


def task_for(scenario: str, root: Path = FIXTURES) -> AgentTask:
    m = meta(scenario, root)
    start, end = datetime.fromisoformat(m["start"]), datetime.fromisoformat(m["end"])
    return task_for_window(scenario, start, end)


def run_agent(
    scenario: str,
    llm: FakeLLMProvider | None = None,
    *,
    env: str = ENV,
    root: Path = FIXTURES,
) -> AgentResult:
    settings = load_settings(env, CONFIG)
    llm = llm or FakeLLMProvider(responder=echo_responder("no_signal"))
    deps = build_deps(settings, llm=llm, replay_dir=root / scenario)
    return asyncio.run(LogAgent(deps).run(task_for(scenario, root)))


def test_s0_and_s1_were_recorded_on_loki() -> None:
    assert {"S0", "S1"} <= set(RECORDED)
    for scenario in RECORDED:
        m = meta(scenario)
        assert m["scenario"] == scenario and m["environment"] == ENV


@pytest.mark.parametrize("scenario", RECORDED)
def test_scenario_ground_truth_on_loki(scenario: str) -> None:
    """The SAME scenario expectations as every other backend; the 'LLM' claims no_signal."""
    result = run_agent(scenario)
    card = score_agent(scenario, result, SCENARIOS[scenario].agents["logs"])
    assert card.passed, [f"{c.name}: {c.detail}" for c in card.checks if not c.passed]
    assert result.usage.total_tokens == 0


def test_every_call_is_logql_through_loki_mcp_and_scoped_to_the_service() -> None:
    result = run_agent("S1")
    calls = result.tool_calls
    assert [c.tool for c in calls] == ["query", "query", "query", "query_range"]
    for call in calls:
        query = call.arguments["query"]
        assert '{namespace="prod", app="payment-service"' in query
        assert "FROM " not in query  # no ES|QL anywhere
    link = result.evidence[0].link or ""
    assert link.startswith("http://localhost:3000/explore?") and "loki" in link


def test_s1_on_loki_db_timeouts_and_v182_deployment() -> None:
    llm = FakeLLMProvider(responder=echo_responder("success"))
    result = run_agent("S1", llm)
    assert result.status is AgentStatus.SUCCESS
    assert set(result.signals) >= S1_SIGNALS, result.signals
    _volume, _patterns, versions, first = result.evidence
    assert "v1.8.2 at" in versions.summary
    assert first.summary.startswith("First occurrence of 'Database connection timeout")
    assert "trace ids ['" in first.summary
    user = llm.requests[0]["messages"][1].content
    assert "could not acquire a connection from the pool" in user
    assert "Deployment detected: version v1.8.2" in user
    system = llm.requests[0]["messages"][0].content
    assert "Query language: Grafana Loki (LogQL)" in system
    assert "MUST select this service" in system


def test_s0_on_loki_has_no_false_positives() -> None:
    result = run_agent(
        "S0", FakeLLMProvider(responder=echo_responder("no_signal", ["error_rate_up"]))
    )
    assert result.status is AgentStatus.NO_SIGNAL
    assert not {"db_timeout_errors_up", "error_rate_up", "new_error_pattern"} & set(result.signals)


@pytest.mark.parametrize("scenario", ["S0", "S1"])
def test_same_conclusions_as_elasticsearch(scenario: str) -> None:
    """Loki reaches the Elasticsearch run's status and incident signals (different live
    windows, so counts differ; the conclusions must not)."""
    loki = run_agent(scenario)
    es = run_agent(scenario, env="local-k8s", root=ES_FIXTURES)
    assert loki.status == es.status
    incident = {"db_timeout_errors_up", "error_rate_up", "new_error_pattern"}
    assert incident & set(loki.signals) == incident & set(es.signals)
