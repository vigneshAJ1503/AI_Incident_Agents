"""Log agent on real Elasticsearch responses recorded from the local stack (S0-S5).

Zero tokens: the fake LLM only submits; everything scored here comes from the
agent's deterministic investigation + guardrails, against scenario ground truth.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aiops.agents.deps import build_deps
from aiops.agents.log_agent import LogAgent
from aiops.core.config import load_settings
from aiops.core.models import AgentResult, AgentStatus, ClaimKind
from aiops.evals.scoring import score_agent
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from tests.fixtures.scenario_context import SCENARIOS, task_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "logs"
CONFIG = Path(__file__).resolve().parents[3] / "config"
EVIDENCE = re.compile(r"\[(ev-[0-9a-f]+)\] (\w+)")


def run_agent(scenario: str, llm: FakeLLMProvider) -> AgentResult:
    import asyncio

    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=llm, replay_dir=FIXTURES / scenario)
    return asyncio.run(LogAgent(deps).run(task_for(scenario)))


def overview(messages: list[ChatMessage]) -> str:
    return (messages[1].content or "").split("## Overview", 1)[1]


def evidence_ids(messages: list[ChatMessage]) -> dict[str, str]:
    return {name: eid for eid, name in EVIDENCE.findall(overview(messages))}


def echo_responder(status: str, signals: list[str] | None = None):  # type: ignore[no-untyped-def]
    """Submit the overview as the summary, citing the patterns evidence."""

    def respond(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
        ids = evidence_ids(messages)
        return tool_call(
            "submit",
            {
                "status": status,
                "summary": overview(messages)[:1500],
                "findings": [
                    {
                        "kind": "OBSERVATION",
                        "type": "log_overview",
                        "description": "Deterministic overview",
                        "evidence_ids": [ids.get("patterns") or ids["volume"]],
                    }
                ],
                "signals": signals or [],
                "confidence": 0.5,
            },
        )

    return respond


@pytest.mark.parametrize("scenario", ["S0", "S1", "S2", "S3", "S4", "S5"])
def test_scenario_ground_truth(scenario: str) -> None:
    """Even an LLM that says 'no_signal' can't hide anomalies; S0 stays clean."""
    result = run_agent(scenario, FakeLLMProvider(responder=echo_responder("no_signal")))
    card = score_agent(scenario, result, SCENARIOS[scenario].agents["logs"])
    failed = [f"{c.name}: {c.detail}" for c in card.checks if not c.passed]
    assert card.passed, failed


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
    import asyncio

    task = task_for("S1")
    task = task.model_copy(update={"context": task.context.model_copy(update={"service": None})})
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=FakeLLMProvider(), replay_dir=FIXTURES / "S1")
    result = asyncio.run(LogAgent(deps).run(task))
    assert result.status is AgentStatus.FAILED
    assert "No log index configured" in result.summary
    assert result.tool_calls == []
