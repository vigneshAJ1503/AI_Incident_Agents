"""Tickets agent on real mock-tickets-mcp responses recorded at FIXED_NOW (S0-S5).

Zero tokens: the fake LLM only submits (or misbehaves on purpose); everything scored here
comes from the agent's deterministic search + relevance rules against scenario ground truth.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aiops.agents.deps import build_deps
from aiops.agents.tickets_agent import TicketsAgent
from aiops.core.config import load_settings
from aiops.core.models import AgentResult, AgentStatus, AgentTask, EvidenceKind
from aiops.evals.replay import echo_responder
from aiops.evals.runner import evaluate_result
from aiops.evals.scoring import score_agent
from aiops.llm.fake import FakeLLMProvider
from tests.fixtures.record_tickets import variants
from tests.fixtures.scenario_context import SCENARIOS, task_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tickets"
CONFIG = Path(__file__).resolve().parents[3] / "config"


def run_agent(fixture: str, llm: FakeLLMProvider, task: AgentTask | None = None) -> AgentResult:
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=llm, replay_dir=FIXTURES / fixture)
    return asyncio.run(TicketsAgent(deps).run(task or task_for(fixture, "tickets")))


def keys(result: AgentResult, category: str) -> list[str]:
    return sorted(
        e.data["key"]
        for e in result.evidence
        if e.kind is EvidenceKind.TICKET and e.data.get("category") == category
    )


@pytest.mark.parametrize("scenario", ["S0", "S1", "S2", "S3", "S4", "S5"])
def test_scenario_ground_truth(scenario: str) -> None:
    """Even an LLM that says 'no_signal' can't hide a known issue; S0 claims none."""
    result = run_agent(scenario, FakeLLMProvider(responder=echo_responder("no_signal")))
    card = score_agent(scenario, result, SCENARIOS[scenario].agents["tickets"])
    failed = [f"{c.name}: {c.detail}" for c in card.checks if not c.passed]
    assert card.passed, failed


def test_s1_finds_ops_12_as_known_issue() -> None:
    llm = FakeLLMProvider(responder=echo_responder("success"))
    result = run_agent("S1", llm)
    assert result.status is AgentStatus.SUCCESS
    assert result.signals == ["known_issue_open", "related_open_tickets"]
    assert keys(result, "known_issue") == ["OPS-12"]
    ops12 = next(e for e in result.evidence if e.data.get("key") == "OPS-12")
    assert ops12.link == "http://localhost:8109/browse/OPS-12"
    assert ops12.data["matched_symptoms"] == ["http_500"]
    assert ops12.summary.startswith("OPS-12 [Open, open] payment-service DB connection timeouts")

    # Two deterministic searches with catalog identifiers + symptom terms, absolute dates.
    scope, keyword = (c.arguments["jql"] for c in result.tool_calls)
    assert [c.tool for c in result.tool_calls] == ["jira_search", "jira_search"]
    assert scope == (
        'project = "OPS" AND (component in ("payments", "identity") OR labels in '
        '("payment-service", "user-service")) AND (statusCategory != Done OR resolved >= '
        '"2026-06-27") ORDER BY updated DESC'
    )
    assert keyword == (
        'project = "OPS" AND (text ~ "500") AND (statusCategory != Done OR resolved >= '
        '"2026-06-27") ORDER BY updated DESC'
    )
    user = llm.requests[0]["messages"][1].content
    assert "KNOWN ISSUES (open, same service, symptom match):" in user
    assert "OPS-12 | Open | Bug | High | payment-service" in user
    system = llm.requests[0]["messages"][0].content
    assert "Jira project `OPS`" in system and "`known_issue_open`" in system
    assert result.prompt_version and result.prompt_version.startswith("tickets/v1@")


def test_s0_llm_cannot_claim_a_known_issue() -> None:
    """The LLM says 'success' + known_issue_open for the healthy baseline: overridden."""
    result = run_agent(
        "S0",
        FakeLLMProvider(
            responder=echo_responder("success", ["known_issue_open", "similar_past_incident"])
        ),
    )
    assert result.status is AgentStatus.NO_SIGNAL
    assert result.signals == ["related_open_tickets"]
    assert keys(result, "known_issue") == []
    # OPS-12 is visible as context, never as a known issue.
    assert "OPS-12" in keys(result, "open_on_service")
    assert len(result.tool_calls) == 1  # no symptom terms -> no keyword search


def test_s0_is_not_an_eval_false_positive() -> None:
    result = run_agent("S0", FakeLLMProvider(responder=echo_responder("no_signal")))
    evaluation = evaluate_result(SCENARIOS["S0"], "tickets", result)
    assert evaluation.passed and not evaluation.false_positive


def test_s3_dependency_issues_and_ops_outside_scope() -> None:
    result = run_agent("S3", FakeLLMProvider(responder=echo_responder("no_signal")))
    assert result.status is AgentStatus.SUCCESS
    assert keys(result, "dependency_known_issue") == ["OPS-12", "OPS-7"]
    ops7 = next(e for e in result.evidence if e.data.get("key") == "OPS-7")
    assert ops7.data["related_service"] == "inventory-service"
    # 200-day-old OPS-2 is outside the look-back; WEB-1 is outside the project.
    all_keys = {e.data.get("key") for e in result.evidence}
    assert "OPS-2" not in all_keys and "WEB-1" not in all_keys


def test_s2_with_logs_symptoms_finds_past_oom_incident() -> None:
    task = variants()["S2-oom-symptoms"]
    result = run_agent("S2-oom-symptoms", FakeLLMProvider(responder=echo_responder()), task)
    assert result.status is AgentStatus.SUCCESS
    assert "similar_past_incident" in result.signals
    assert keys(result, "similar_past") == ["OPS-3"]


def test_s5_similar_past_incident_not_known_issue() -> None:
    result = run_agent("S5", FakeLLMProvider(responder=echo_responder()))
    assert keys(result, "similar_past") == ["OPS-5"]
    assert keys(result, "known_issue") == []
    assert "OPS-7" in keys(result, "keyword_other")  # "slow query" on an unrelated service


def test_nothing_to_search_by_fails_cleanly() -> None:
    task = task_for("S0", "tickets")
    task = task.model_copy(
        update={
            "context": task.context.model_copy(
                update={"service": None, "question": "Is anything wrong?"}
            )
        }
    )
    result = run_agent("S0", FakeLLMProvider(), task)
    assert result.status is AgentStatus.FAILED
    assert "Nothing to search tickets by" in result.summary
    assert result.tool_calls == []
