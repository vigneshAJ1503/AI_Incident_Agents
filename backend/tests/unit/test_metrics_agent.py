"""Metrics agent on real prometheus-mcp responses recorded during LIVE faults (S0-S5).

Zero tokens: the fake LLM only submits; everything scored here comes from the agent's
deterministic investigation + guardrails, against scenario ground truth. Each fixture
directory has a meta.json with the recorded window and the real injection time.
Fixtures: `make record-metrics S=S1` (see tests/fixtures/record_metrics.py).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aiops.agents.deps import build_deps
from aiops.agents.metrics_agent import MetricsAgent
from aiops.core.config import load_settings
from aiops.core.models import AgentResult, AgentStatus, AgentTask, ClaimKind
from aiops.evals.replay import ReplayMeta, echo_responder, replay_task
from aiops.evals.runner import EvalReport, run_eval
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from tests.fixtures.scenario_context import SCENARIOS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "metrics"
CONFIG = Path(__file__).resolve().parents[3] / "config"
SCENARIO_IDS = ["S0", "S1", "S2", "S3", "S4", "S5"]


def meta(scenario: str) -> ReplayMeta:
    found = ReplayMeta.load(FIXTURES / scenario)
    assert found is not None, f"missing {FIXTURES / scenario / 'meta.json'}"
    return found


def task_for(scenario: str) -> AgentTask:
    meta(scenario)  # fail clearly if meta.json is missing
    return replay_task(SCENARIOS[scenario], "metrics", FIXTURES / scenario)


def run_agent(scenario: str, llm: FakeLLMProvider, task: AgentTask | None = None) -> AgentResult:
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=llm, replay_dir=FIXTURES / scenario)
    return asyncio.run(MetricsAgent(deps).run(task or task_for(scenario)))


def evidence_for(result: AgentResult, metric: str) -> dict[str, Any]:
    ev = next(e for e in result.evidence if e.data.get("metric") == metric)
    return ev.data


def start_of(data: dict[str, Any], service: str) -> datetime:
    value = data["services"][service]["start_time"]
    assert value, (data["metric"], service, data["services"][service])
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def replay_report() -> EvalReport:
    """The shared eval runner in replay mode (the same code as `make eval AGENT=metrics`)."""
    with pytest.MonkeyPatch.context() as mp:  # module scope: isolate from a developer .env
        mp.setattr("aiops.core.config.load_dotenv", lambda *a, **k: False)
        settings = load_settings("local", CONFIG)
    return asyncio.run(run_eval(settings, "metrics", mode="replay"))


@pytest.mark.parametrize("scenario", SCENARIO_IDS)
def test_scenario_ground_truth(replay_report: EvalReport, scenario: str) -> None:
    """An LLM that says 'no_signal' can't hide an anomaly; S0 stays clean."""
    result = next(r for r in replay_report.results if r.scenario == scenario)
    assert result.passed, [f"{c.name}: {c.detail}" for c in result.failed_checks]
    assert not result.false_positive
    assert result.tokens == 0 and result.llm_calls == 1


def test_replay_report_aggregates(replay_report: EvalReport) -> None:
    summary = replay_report.summary
    assert summary.pass_rate == 1.0 and summary.scenarios == 6
    assert summary.healthy_scenarios == 1 and summary.false_positive_rate == 0.0
    assert summary.citation_validity == 1.0


def test_fixtures_are_live_recordings() -> None:
    for scenario in SCENARIO_IDS:
        m = meta(scenario)
        assert m.scenario == scenario
        assert m.end - m.start == timedelta(minutes=15)
        if scenario == "S0":
            assert m.incident_start is None
        else:  # recorded ~6 minutes after the real injection
            assert m.incident_start is not None and m.start < m.incident_start < m.end


def test_s1_detects_the_pool_exhaustion_with_its_start_time() -> None:
    result = run_agent("S1", FakeLLMProvider(responder=echo_responder("success")))
    assert result.status is AgentStatus.SUCCESS
    assert {"error_rate_up", "db_pool_saturated", "latency_up"} <= set(result.signals)
    # 10 deterministic range queries, service + dependencies in each
    assert [c.tool for c in result.tool_calls] == ["query_range"] * 10
    args = result.tool_calls[0].arguments
    m = meta("S1")
    assert args["step"] == "30s"
    assert args["start"] == (m.start - timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
    assert 'service=~"payment-service|user-service"' in args["query"]
    incident = m.incident_start
    assert incident is not None
    errors = evidence_for(result, "error_rate")
    pool = evidence_for(result, "db_pool_utilization")
    latency = evidence_for(result, "latency_p95")
    for data in (errors, pool, latency):
        delay = start_of(data, "payment-service") - incident
        assert timedelta(0) <= delay <= timedelta(minutes=2), (data["metric"], delay)
    # UC-05: the p95 spike is >= 5x its baseline
    assert latency["services"]["payment-service"]["ratio"] >= 5
    assert errors["baseline"] == 0 and errors["current"] > 0.05
    # evidence shape: numbers, window, chart series and a Grafana panel link
    ev = next(e for e in result.evidence if e.data["metric"] == "error_rate")
    assert ev.kind.value == "metric" and ev.timestamp is not None
    assert ev.link and "/d/aiops-service-overview/" in ev.link and "viewPanel=6" in ev.link
    assert ev.data["query_link"].startswith("http://localhost:9090/query?g0.expr=")
    assert set(ev.data["series"]) == {"payment-service", "user-service"}
    assert 0 < len(ev.data["series"]["payment-service"]) <= 41
    assert "payment-service error rate up" in ev.summary


def test_prompt_is_vendor_neutral_with_provider_guidance() -> None:
    """PromQL and tool names come from the prometheus provider's fragment (ADR-0012)."""
    llm = FakeLLMProvider(responder=echo_responder("success"))
    result = run_agent("S1", llm)
    system = llm.requests[0]["messages"][0].content
    assert result.prompt_version and result.prompt_version.startswith("metrics/v2@")
    assert "+providers/metrics/prometheus/v1@" in result.prompt_version
    assert "# Query language: Prometheus (PromQL)" in system
    assert "`$provider_guidance`" not in system and "$provider_guidance" not in system
    agent_prompt = (CONFIG / "prompts" / "metrics" / "v2.md").read_text()
    for vendor_term in ("PromQL", "query_range", "histogram_quantile", "Prometheus"):
        assert vendor_term not in agent_prompt


def test_s3_cross_service_latency() -> None:
    result = run_agent("S3", FakeLLMProvider(responder=echo_responder("success")))
    assert {"latency_up", "dependency_latency_up"} <= set(result.signals)
    latency = evidence_for(result, "latency_p95")
    inventory = start_of(latency, "inventory-service")
    order = start_of(latency, "order-service")
    assert inventory <= order  # the dependency slowed down first (or in the same step)
    assert "Dependency inventory-service" in result.summary


def test_s2_memory_leak_then_oom_restarts() -> None:
    result = run_agent("S2", FakeLLMProvider(responder=echo_responder("success")))
    assert "memory_pressure" in result.signals
    incident = meta("S2").incident_start
    assert incident is not None
    rss = evidence_for(result, "memory_rss")
    assert rss["services"]["order-service"]["ratio"] >= 1.5
    assert timedelta(0) <= start_of(rss, "order-service") - incident <= timedelta(minutes=2)
    oom = evidence_for(result, "oom_killed")
    assert oom["services"]["order-service"]["anomalous"]
    assert "OOMKilled" in next(e.summary for e in result.evidence if e.data is oom)
    # the leak is order-service's own: its dependencies stay flat
    assert not rss["services"]["payment-service"]["anomalous"]


def test_s4_stuck_rollout_is_invisible_in_metrics() -> None:
    """The old ReplicaSet keeps serving: no error/latency impact on user-service, and no
    anomaly that starts after the injection (that root cause is the K8s agent's)."""
    result = run_agent("S4", FakeLLMProvider(responder=echo_responder()))
    incident = meta("S4").incident_start
    assert incident is not None
    for metric in ("error_rate", "latency_p95", "memory_rss", "restarts"):
        assert not evidence_for(result, metric)["services"]["user-service"]["anomalous"]
    for data in (e.data for e in result.evidence):
        stats = data["services"].get("user-service", {})
        if stats.get("anomalous"):
            assert datetime.fromisoformat(stats["start_time"].replace("Z", "+00:00")) < incident


def test_s5_cache_down_everywhere() -> None:
    result = run_agent("S5", FakeLLMProvider(responder=echo_responder("success")))
    assert "cache_down" in result.signals
    cache = evidence_for(result, "cache_up")
    assert cache["services"]["payment-service"]["anomalous"]


def test_s0_says_no_anomaly_explicitly() -> None:
    result = run_agent("S0", FakeLLMProvider(responder=echo_responder("no_signal")))
    assert result.status is AgentStatus.NO_SIGNAL
    assert result.signals == ["no_anomaly"]
    assert "No metric anomaly for payment-service" in result.summary


def _fresh_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a cluster that started at the window start: drop every recorded point
    before it, so no series has a baseline (the live-demo situation)."""
    original = MetricsAgent._stats

    def without_baseline(mr: Any, service: str, values: Any, window: Any, absolute: Any) -> Any:
        start = window.start.timestamp()
        fresh = [(t, v) for t, v in values if t >= start]
        return original(mr, service, fresh, window, absolute)

    monkeypatch.setattr(MetricsAgent, "_stats", staticmethod(without_baseline))


def test_fresh_cluster_s1_uses_absolute_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh_cluster(monkeypatch)
    result = run_agent("S1", FakeLLMProvider(responder=echo_responder("no_signal")))
    assert result.status is AgentStatus.SUCCESS  # the data wins over the LLM's "no_signal"
    assert {"error_rate_up", "latency_up", "db_pool_saturated"} <= set(result.signals)
    assert "no_anomaly" not in result.signals
    for metric in ("error_rate", "latency_p95", "db_pool_utilization", "db_pool_pending"):
        own = evidence_for(result, metric)["services"]["payment-service"]
        assert own["anomalous"] and own["basis"] == "absolute", (metric, own)
        assert own["baseline"] is None  # never invented
    errors = next(e for e in result.evidence if e.data.get("metric") == "error_rate")
    assert "no baseline; above the absolute threshold 5.0%" in errors.summary


def test_fresh_cluster_s0_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh_cluster(monkeypatch)
    result = run_agent("S0", FakeLLMProvider(responder=echo_responder("no_signal")))
    assert result.status is AgentStatus.NO_SIGNAL
    assert result.signals == ["no_anomaly"]
    assert "No metric anomaly for payment-service" in result.summary


def test_bad_absolute_thresholds_fail_readably() -> None:
    settings = load_settings("local", CONFIG)
    settings.capabilities["metrics"].settings["absolute_thresholds"] = {"latency": 2}
    deps = build_deps(settings, llm=FakeLLMProvider(), replay_dir=FIXTURES / "S1")
    result = asyncio.run(MetricsAgent(deps).run(task_for("S1")))
    assert result.status is AgentStatus.FAILED
    assert "absolute_thresholds: unknown metric(s) latency" in (result.error or result.summary)


def test_llm_cannot_invent_or_hide_signals() -> None:
    def overclaiming(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
        return tool_call(
            "submit",
            {
                "status": "success",
                "summary": "The database is the root cause.",
                "findings": [
                    {"kind": "HYPOTHESIS", "type": "root_cause", "description": "DB is down"}
                ],
                "signals": ["error_rate_up", "cache_down"],
                "confidence": 0.9,
            },
        )

    healthy = run_agent("S0", FakeLLMProvider(responder=overclaiming))
    assert healthy.status is AgentStatus.NO_SIGNAL and healthy.signals == ["no_anomaly"]
    assert healthy.summary.startswith("No metric anomaly for payment-service")
    assert healthy.findings[0].kind is ClaimKind.HYPOTHESIS

    hiding = run_agent("S1", FakeLLMProvider(responder=echo_responder("no_signal")))
    assert hiding.status is AgentStatus.SUCCESS and "error_rate_up" in hiding.signals


def test_unknown_service_fails_cleanly() -> None:
    base = task_for("S0")
    task = base.model_copy(
        update={"context": base.context.model_copy(update={"service": "billing-service"})}
    )
    result = run_agent("S0", FakeLLMProvider(responder=echo_responder()), task)
    assert result.status is AgentStatus.FAILED and "billing-service" in (result.error or "")
