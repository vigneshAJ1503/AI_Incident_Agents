"""Log agent on REAL Kubernetes logs (PR-016): AIOPS_ENV=local-k8s, zero code change.

Fixtures were recorded live from Fluent Bit -> Elasticsearch (logs-k8s-*) while
`aiops fault run` held the cluster lock (tests/fixtures/record_logs_k8s.py). A live
window isn't at a fixed time, so each fixture dir has a meta.json with the window used.
Zero tokens: the fake LLM only submits; the result is scored on the deterministic phase.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aiops.agents.deps import build_deps
from aiops.agents.log_agent import LogAgent
from aiops.agents.log_agent.agent import LogScope
from aiops.core.config import load_settings
from aiops.core.models import AgentResult, AgentStatus, AgentTask
from aiops.evals.replay import echo_responder
from aiops.evals.scoring import score_agent
from aiops.llm.fake import FakeLLMProvider
from tests.fixtures.record_logs_k8s import task_for_window
from tests.fixtures.scenario_context import SCENARIOS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "logs-k8s"
CONFIG = Path(__file__).resolve().parents[3] / "config"
ENV = "local-k8s"
RECORDED = sorted(p.parent.name for p in FIXTURES.glob("*/meta.json"))


def meta(scenario: str) -> dict[str, str]:
    data: dict[str, str] = json.loads((FIXTURES / scenario / "meta.json").read_text())
    return data


def task_for(scenario: str) -> AgentTask:
    """The exact task recorded: the scenario's question over the stored live window."""
    m = meta(scenario)
    start, end = datetime.fromisoformat(m["start"]), datetime.fromisoformat(m["end"])
    return task_for_window(scenario, start, end)


def run_agent(scenario: str, llm: FakeLLMProvider | None = None) -> AgentResult:
    settings = load_settings(ENV, CONFIG)
    llm = llm or FakeLLMProvider(responder=echo_responder("no_signal"))
    deps = build_deps(settings, llm=llm, replay_dir=FIXTURES / scenario)
    return asyncio.run(LogAgent(deps).run(task_for(scenario)))


def test_s0_and_s1_were_recorded() -> None:
    assert {"S0", "S1"} <= set(RECORDED)
    for scenario in RECORDED:
        m = meta(scenario)
        assert m["scenario"] == scenario and m["environment"] == ENV


@pytest.mark.parametrize("scenario", RECORDED)
def test_scenario_ground_truth_on_real_logs(scenario: str) -> None:
    """Same ground truth as the synthetic runs; the 'LLM' claims no_signal (strictest)."""
    result = run_agent(scenario)
    card = score_agent(scenario, result, SCENARIOS[scenario].agents["logs"])
    assert card.passed, [f"{c.name}: {c.detail}" for c in card.checks if not c.passed]
    assert result.usage.total_tokens == 0


def test_every_query_is_filtered_to_the_service() -> None:
    result = run_agent("S1")
    queries = [c.arguments["query"] for c in result.tool_calls]
    assert len(queries) == 4
    assert all(
        q.startswith('FROM logs-k8s-* | WHERE kubernetes.labels.app == "payment-service" | ')
        for q in queries
    )
    link = result.evidence[0].link or ""
    assert "kubernetes.labels.app%3A%22payment-service%22" in link


def test_s1_real_db_timeouts_and_v182_deployment() -> None:
    llm = FakeLLMProvider(responder=echo_responder("success"))
    result = run_agent("S1", llm)
    assert result.status is AgentStatus.SUCCESS
    assert {"db_timeout_errors_up", "deployment_detected", "new_error_pattern"} <= set(
        result.signals
    )
    _volume, _patterns, versions, first = result.evidence
    assert "v1.8.2 at" in versions.summary
    assert first.summary.startswith("First occurrence of 'Database connection timeout")
    user = llm.requests[0]["messages"][1].content
    assert "could not acquire a connection from the pool" in user
    assert "Deployment detected: version v1.8.2" in user
    system = llm.requests[0]["messages"][0].content
    assert "The index is shared by all services" in system


def test_s0_real_logs_have_no_false_positives() -> None:
    result = run_agent(
        "S0", FakeLLMProvider(responder=echo_responder("no_signal", ["error_rate_up"]))
    )
    assert result.status is AgentStatus.NO_SIGNAL
    assert not {"db_timeout_errors_up", "error_rate_up", "new_error_pattern"} & set(result.signals)


# -- the service filter itself (backwards compatible) -------------------------------------


def scope(service_value: str | None) -> LogScope:
    return LogScope(
        index="logs-*",
        fields={"service": "kubernetes.labels.app"},
        error_levels=["ERROR"],
        pattern_levels=["ERROR"],
        baseline=timedelta(hours=2),
        startup_pattern="Starting*",
        link_template=None,
        service_value=service_value,
    )


def test_scope_without_filter_is_unchanged() -> None:
    plain = scope(None)
    assert plain.source == "FROM logs-*" and plain.service_where is None
    assert plain.service_kql == ""


def test_scope_filter_escapes_the_value() -> None:
    shared = scope('pay"ment')
    assert shared.source == 'FROM logs-* | WHERE kubernetes.labels.app == "pay\\"ment"'


def test_synthetic_local_env_has_no_service_filter() -> None:
    settings = load_settings("local", CONFIG)
    assert not settings.capability("logs").settings.get("service_filter")
