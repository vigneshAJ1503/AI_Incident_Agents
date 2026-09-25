"""Log agent on real Elasticsearch responses recorded from the local stack (S0-S5).

Zero tokens: the fake LLM only submits; everything scored here comes from the
agent's deterministic investigation + guardrails, against scenario ground truth.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aiops.agents.deps import build_deps
from aiops.agents.log_agent import LogAgent
from aiops.core.config import load_settings
from aiops.core.models import AgentResult, AgentStatus, ClaimKind
from aiops.evals.replay import echo_responder
from aiops.evals.runner import EvalReport, run_eval
from aiops.llm.fake import FakeLLMProvider
from tests.fixtures.scenario_context import task_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "logs"
CONFIG = Path(__file__).resolve().parents[3] / "config"


def run_agent(scenario: str, llm: FakeLLMProvider) -> AgentResult:
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=llm, replay_dir=FIXTURES / scenario)
    return asyncio.run(LogAgent(deps).run(task_for(scenario)))


@pytest.fixture(scope="module")
def replay_report() -> EvalReport:
    """The shared eval runner in replay mode (the same code as `make eval AGENT=logs`)."""
    with pytest.MonkeyPatch.context() as mp:  # module scope: isolate from a developer .env
        mp.setattr("aiops.core.config.load_dotenv", lambda *a, **k: False)
        settings = load_settings("local", CONFIG)
    return asyncio.run(run_eval(settings, "logs", mode="replay"))


@pytest.mark.parametrize("scenario", ["S0", "S1", "S2", "S3", "S4", "S5"])
def test_scenario_ground_truth(replay_report: EvalReport, scenario: str) -> None:
    """Even an LLM that says 'no_signal' can't hide anomalies; S0 stays clean."""
    result = next(r for r in replay_report.results if r.scenario == scenario)
    assert result.passed, [f"{c.name}: {c.detail}" for c in result.failed_checks]
    assert not result.false_positive
    assert result.tokens == 0 and result.llm_calls == 1


def test_replay_report_aggregates(replay_report: EvalReport) -> None:
    summary = replay_report.summary
    assert [r.scenario for r in replay_report.results] == ["S0", "S1", "S2", "S3", "S4", "S5"]
    assert summary.pass_rate == 1.0
    assert summary.healthy_scenarios == 1 and summary.false_positive_rate == 0.0
    assert summary.citation_validity == 1.0 and summary.findings == 6


def test_llm_cannot_add_data_signals_to_healthy_s0() -> None:
    result = run_agent(
        "S0",
        FakeLLMProvider(
            responder=echo_responder("no_signal", ["error_rate_up", "db_timeout_errors_up"])
        ),
    )
    assert result.signals == []


def test_llm_semantic_signal_kept_when_data_is_anomalous() -> None:
    result = run_agent(
        "S4", FakeLLMProvider(responder=echo_responder("success", ["upstream_unavailable"]))
    )
    assert result.signals == ["capacity_degraded", "upstream_unavailable"]


def test_s1_details() -> None:
    llm = FakeLLMProvider(responder=echo_responder("success"))
    result = run_agent("S1", llm)
    assert result.status is AgentStatus.SUCCESS
    assert [c.tool for c in result.tool_calls] == ["execute_esql"] * 4
    volume, patterns, versions, first = result.evidence
    assert volume.summary.startswith(
        "62 errors in 389 lines during the incident window vs 80 in 17367"
    )
    assert "1 anomalous" in patterns.summary or "2 anomalous" in patterns.summary
    assert "v1.8.2 at 2026-09-25T10:08:00.000Z" in versions.summary
    assert first.summary.startswith("First occurrence of 'Database connection timeout")
    assert "96a931cfdc9709fe" in first.summary
    assert all(e.link and "localhost:5601" in e.link for e in result.evidence)

    request = llm.requests[0]
    system, user = request["messages"][0].content, request["messages"][1].content
    assert "Baseline window (UTC): 2026-09-24T10:00:00Z to 2026-09-25T10:00:00Z" in system
    assert "`cache_connection_errors`" in system  # signal vocabulary rendered
    assert (
        "Database connection timeout: could not acquire a connection from the pool within <NUM>ms"
        in user
    )
    assert "| NEW |" in user and "Deployment detected: version v1.8.2" in user
    assert result.prompt_version and result.prompt_version.startswith("logs/v2@")
    assert result.findings[0].kind is ClaimKind.OBSERVATION


def test_unknown_service_index_fails_cleanly() -> None:
    task = task_for("S1")
    task = task.model_copy(update={"context": task.context.model_copy(update={"service": None})})
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=FakeLLMProvider(), replay_dir=FIXTURES / "S1")
    result = asyncio.run(LogAgent(deps).run(task))
    assert result.status is AgentStatus.FAILED
    assert "No log index configured" in result.summary
    assert result.tool_calls == []
