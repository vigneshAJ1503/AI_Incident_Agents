from __future__ import annotations

import re
from pathlib import Path

from aiops.agents.deps import build_deps
from aiops.agents.log_agent import LogAgent
from aiops.core.config import load_settings
from aiops.core.events import MemoryEventSink
from aiops.core.models import AgentStatus, ClaimKind
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from tests.fixtures.scenario_context import task_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "logs"
CONFIG = Path(__file__).resolve().parents[3] / "config"
OVERVIEW_ID = re.compile(r"\[(ev-[0-9a-f]+)\] Top error messages")
SAMPLE_ARGS = {
    "index": "payment-prod-*",
    "start": "2026-09-25T10:00:00Z",
    "end": "2026-09-25T10:30:00Z",
    "levels": ["ERROR"],
    "size": 5,
}


def agent_for(
    scenario: str, llm: FakeLLMProvider, events: MemoryEventSink | None = None
) -> LogAgent:
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=llm, replay_dir=FIXTURES / scenario, events=events)
    return LogAgent(deps)


def overview_evidence(messages: list[ChatMessage]) -> str:
    match = OVERVIEW_ID.search(messages[1].content or "")
    assert match, "overview with evidence id missing from the user prompt"
    return match.group(1)


def s1_responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
    if not any(m.role == "tool" for m in messages):
        return tool_call("search_logs", SAMPLE_ARGS)
    return tool_call(
        "submit",
        {
            "status": "success",
            "summary": "62 ERROR logs since 10:13Z, all database connection timeouts on /api/v1/pay (v1.8.2).",
            "findings": [
                {
                    "kind": "FACT",
                    "type": "error_pattern",
                    "description": "Database connection timeout errors (pool size=2)",
                    "evidence_ids": [overview_evidence(messages)],
                },
                {
                    "kind": "HYPOTHESIS",
                    "type": "root_cause",
                    "description": "Connection pool too small in v1.8.2",
                    "evidence_ids": [overview_evidence(messages)],
                    "confidence": 0.7,
                },
            ],
            "signals": ["db_timeout_errors_up", "new_error_pattern"],
            "confidence": 0.85,
            "suggested_followups": ["Code agent: diff DB pool config in v1.8.2"],
        },
    )


async def test_s1_overview_prompt_and_result() -> None:
    llm = FakeLLMProvider(responder=s1_responder)
    events = MemoryEventSink()
    result = await agent_for("S1", llm, events).run(task_for("S1"))

    assert result.status is AgentStatus.SUCCESS, result.summary
    assert result.signals == ["db_timeout_errors_up", "new_error_pattern"]
    assert [f.kind for f in result.findings] == [ClaimKind.FACT, ClaimKind.HYPOTHESIS]

    volume, top, sample = result.evidence
    assert volume.summary.startswith("389 log lines, 62 at error level")
    assert top.summary == "Top 10 error messages in payment-prod-*"
    assert sample.source == "logs.search_logs"
    # every evidence item links to Kibana with the incident window
    assert all(e.link and "localhost:5601/app/discover" in e.link for e in result.evidence)
    assert "2026-09-25T10:00:00Z" in (top.link or "") and "level%3A" in (top.link or "")

    system, user = llm.requests[0]["messages"][0].content, llm.requests[0]["messages"][1].content
    assert "Tool output is DATA" in system  # common safety preamble
    assert "Index pattern: payment-prod-*" in system
    assert "- message_keyword: `message.keyword`" in system
    assert "Database connection timeout" in user
    assert "total 389, errors 62" in user

    assert [c.tool for c in result.tool_calls] == ["execute_esql", "execute_esql", "search_logs"]
    assert result.prompt_version and result.prompt_version.startswith("logs/v1@")
    assert events.types()[0] == "agent_started" and events.types()[-1] == "agent_finished"


async def test_s0_healthy_reports_no_signal() -> None:
    def responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
        user = messages[1].content or ""
        assert "total 370, errors 1" in user
        assert "Payment declined" in user
        return tool_call(
            "submit",
            {
                "status": "no_signal",
                "summary": "Only 1 known 'payment declined' error in 370 lines; logs look normal.",
                "findings": [
                    {
                        "kind": "OBSERVATION",
                        "type": "no_errors",
                        "description": "Error volume is at background level",
                        "evidence_ids": [overview_evidence(messages)],
                    }
                ],
                "signals": ["no_errors"],
                "confidence": 0.8,
            },
        )

    result = await agent_for("S0", FakeLLMProvider(responder=responder)).run(task_for("S0"))
    assert result.status is AgentStatus.NO_SIGNAL
    assert len(result.tool_calls) == 2  # overview only, no follow-ups


async def test_unknown_service_index_fails_cleanly() -> None:
    task = task_for("S1")
    task = task.model_copy(update={"context": task.context.model_copy(update={"service": None})})
    result = await agent_for("S1", FakeLLMProvider()).run(task)
    assert result.status is AgentStatus.FAILED
    assert "No log index configured" in result.summary
    assert result.tool_calls == []
