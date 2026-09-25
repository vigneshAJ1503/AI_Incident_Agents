"""Knowledge agent on real knowledge-mcp responses recorded from the local stack (S0-S5).

Zero tokens. Each scenario is recorded with the hints from
``scenarios/<id>/agents/knowledge.yaml`` (representative Log agent findings: signals +
top anomalous patterns, checked against the Log agent's own replay below) and without
hints (question only).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from aiops.agents.deps import build_deps
from aiops.agents.knowledge_agent import KnowledgeAgent
from aiops.agents.log_agent import LogAgent
from aiops.core.config import load_settings
from aiops.core.models import AgentResult, AgentStatus, AgentTask, ClaimKind
from aiops.evals.replay import echo_responder
from aiops.evals.runner import EvalReport, run_eval
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from tests.fixtures.scenario_context import SCENARIOS, task_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CONFIG = Path(__file__).resolve().parents[3] / "config"
RUNBOOKS = "knowledge-base/runbooks"
LINK = "https://github.com/vigneshAJ1503/AI_Incident_Agents/blob/main/"


def task(scenario: str, *, hints: bool = True) -> AgentTask:
    t = task_for(scenario, "knowledge")
    return t if hints else t.model_copy(update={"hints": {}})


def run_agent(
    scenario: str, llm: FakeLLMProvider | None = None, *, hints: bool = True
) -> AgentResult:
    settings = load_settings("local", CONFIG)
    llm = llm or FakeLLMProvider(responder=echo_responder())
    deps = build_deps(settings, llm=llm, replay_dir=FIXTURES / "knowledge" / scenario)
    return asyncio.run(KnowledgeAgent(deps).run(task(scenario, hints=hints)))


def top_runbooks(result: AgentResult) -> list[str]:
    return [
        f.description.split(": ", 1)[1].split(" ", 1)[0]
        for f in result.findings
        if f.type == "runbook_match"
    ]


@pytest.fixture(scope="module")
def replay_report() -> EvalReport:
    """The shared eval runner in replay mode (the same code as `make eval AGENT=knowledge`)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("aiops.core.config.load_dotenv", lambda *a, **k: False)
        settings = load_settings("local", CONFIG)
    return asyncio.run(run_eval(settings, "knowledge", mode="replay"))


@pytest.mark.parametrize("scenario", ["S0", "S1", "S2", "S3", "S4", "S5"])
def test_scenario_ground_truth_with_log_hints(replay_report: EvalReport, scenario: str) -> None:
    """An LLM that says 'no_signal' can't hide a documented runbook; S0 stays clean."""
    result = next(r for r in replay_report.results if r.scenario == scenario)
    assert result.passed, [f"{c.name}: {c.detail}" for c in result.failed_checks]
    assert not result.false_positive
    assert result.tokens == 0


@pytest.mark.parametrize(
    ("scenario", "runbook"),
    [
        ("S1", "database-connection-pool.md"),
        ("S2", "memory-leak-oom.md"),
        ("S3", "dependency-timeouts.md"),
        ("S4", "bad-deployment-rollback.md"),
        ("S5", "redis-outage.md"),
    ],
)
def test_expected_runbook_ranks_first_with_hints(scenario: str, runbook: str) -> None:
    result = run_agent(scenario)
    ranked = top_runbooks(result)
    assert ranked[0] == f"{RUNBOOKS}/{runbook}" and len(ranked) <= 3
    assert "known_issue_documented" in result.signals


@pytest.mark.parametrize(
    ("scenario", "status", "first"),
    [
        ("S0", AgentStatus.NO_SIGNAL, None),
        ("S1", AgentStatus.SUCCESS, "high-error-rate.md"),  # "HTTP 500" -> generic triage
        ("S2", AgentStatus.NO_SIGNAL, None),  # "failing intermittently": nothing specific
        ("S3", AgentStatus.SUCCESS, "dependency-timeouts.md"),  # "timing out" -> timeout
        ("S4", AgentStatus.NO_SIGNAL, None),
        ("S5", AgentStatus.SUCCESS, None),  # "slow" -> some latency-related runbook
    ],
)
def test_question_only_without_hints(scenario: str, status: AgentStatus, first: str | None) -> None:
    """Without hints the agent is honest: thin questions find little and never a known issue."""
    result = run_agent(scenario, hints=False)
    assert result.status is status
    assert "known_issue_documented" not in result.signals
    if status is AgentStatus.NO_SIGNAL:
        assert result.signals == ["no_relevant_docs"] and top_runbooks(result) == []
    if first:
        assert top_runbooks(result)[0] == f"{RUNBOOKS}/{first}"


def test_s1_citations_and_tool_sequence() -> None:
    llm = FakeLLMProvider(responder=echo_responder())
    result = run_agent("S1", llm)
    assert [c.tool for c in result.tool_calls] == [
        "list_docs",
        *["search"] * 4,
        *["get_doc"] * 4,  # top 3 runbooks + the payment-service doc
    ]
    search = result.tool_calls[1].arguments
    assert search["services"] == ["payment-service"] and search["k"] == 8
    assert search["query"].startswith("Database connection timeout could not acquire")

    pool = next(
        e
        for e in result.evidence
        if e.data.get("path") == f"{RUNBOOKS}/database-connection-pool.md"
    )
    assert pool.link == f"{LINK}{RUNBOOKS}/database-connection-pool.md#symptoms"
    assert "rollout undo" in pool.data["sections"]["mitigation"]
    assert pool.data["known_issue_matches"][0]["section"].endswith("> Symptoms")
    citations = {c["section"]: c["link"] for c in pool.data["citations"]}
    assert citations["Database connection pool exhaustion > Rollback"].endswith("#rollback")
    service_doc = result.evidence[-1]
    assert service_doc.data["path"] == "knowledge-base/services/payment-service.md"
    assert "#payments-oncall" in service_doc.data["sections"]["Ownership"]

    kinds = [(f.kind, f.type) for f in result.findings]
    assert (ClaimKind.FACT, "runbook_match") in kinds
    assert (ClaimKind.OBSERVATION, "known_issue") in kinds
    assert (ClaimKind.RECOMMENDATION, "documented_mitigation") in kinds
    assert result.prompt_version and result.prompt_version.startswith("knowledge/v1@")

    system, user = (m.content or "" for m in llm.requests[0]["messages"][:2])
    assert "`known_issue_documented`" in system  # signal vocabulary rendered
    assert "1. [ev-" in user and "database-connection-pool.md" in user
    assert "Known issue documented: 'Database connection timeout" in user


def llm_claims(status: str, signals: list[str]):  # type: ignore[no-untyped-def]
    def respond(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
        return tool_call(
            "submit",
            {"status": status, "summary": "LLM view", "signals": signals, "confidence": 0.9},
        )

    return respond


def test_llm_cannot_invent_a_known_issue_for_healthy_s0() -> None:
    result = run_agent(
        "S0",
        FakeLLMProvider(
            responder=llm_claims("success", ["runbook_found", "known_issue_documented"])
        ),
    )
    assert result.status is AgentStatus.NO_SIGNAL
    assert result.signals == ["no_relevant_docs"]
    assert [f.type for f in result.findings] == ["no_relevant_docs"]
    assert "No symptoms were given" in result.findings[0].description


def test_unrecorded_searches_fail_cleanly() -> None:
    """Every search fails (here: not in the fixture) -> failed, not a crash."""
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=FakeLLMProvider(), replay_dir=FIXTURES / "knowledge" / "S1")
    t = task("S1")
    context = t.context.model_copy(update={"question": "Is checkout degraded?"})
    t = t.model_copy(update={"hints": {"patterns": ["never recorded"]}, "context": context})
    result = asyncio.run(KnowledgeAgent(deps).run(t))
    assert result.status is AgentStatus.FAILED
    assert "Could not search the knowledge base" in result.summary


def log_agent_view(scenario: str) -> tuple[list[str], str]:
    """(signals, overview) of the Log agent's replay for the scenario."""
    settings = load_settings("local", CONFIG)
    llm = FakeLLMProvider(responder=echo_responder())
    deps = build_deps(settings, llm=llm, replay_dir=FIXTURES / "logs" / scenario)
    result = asyncio.run(LogAgent(deps).run(task_for(scenario)))
    return result.signals, llm.requests[0]["messages"][1].content or ""


@pytest.mark.parametrize("scenario", ["S0", "S1", "S2", "S3", "S4", "S5"])
def test_scenario_hints_match_the_log_agent(scenario: str) -> None:
    """The hardcoded hints in scenarios/*/agents/knowledge.yaml are what the Log agent reports."""
    hints: dict[str, Any] = SCENARIOS[scenario].agents["knowledge"].hints
    signals, overview = log_agent_view(scenario)
    assert hints["signals"] == signals
    anomalous = (
        overview.split("Anomalous patterns", 1)[1].split("Other patterns")[0]
        if hints["patterns"]
        else ""
    )
    for pattern in hints["patterns"]:
        assert pattern in anomalous, pattern
