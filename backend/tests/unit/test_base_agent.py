from __future__ import annotations

from pathlib import Path

from aiops.agents.registry import AgentRegistry
from aiops.core.models import AgentStatus, ClaimKind
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, text, tool_call
from aiops.mcp.registry import MCPRegistry
from tests.unit.agent_helpers import (
    EchoAgent,
    last_evidence_id,
    make_deps,
    make_server,
    make_settings,
    make_task,
    search_then_submit,
    submit,
)


async def test_happy_path_evidence_cited_and_events(tmp_path: Path) -> None:
    llm = FakeLLMProvider(responder=search_then_submit)
    deps, events, audit = make_deps(tmp_path, llm)
    result = await EchoAgent(deps).run(make_task())

    assert result.status is AgentStatus.SUCCESS
    assert len(result.evidence) == 1
    evidence = result.evidence[0]
    assert evidence.source == "logs.search_logs"
    assert evidence.data["result"]["errors"] == 427
    assert result.findings[0].kind is ClaimKind.FACT
    assert result.findings[0].evidence_ids == [evidence.id]
    assert result.signals == ["db_timeout_errors_up"]
    assert [c.tool for c in result.tool_calls] == ["search_logs"]
    assert result.usage.calls == 2
    assert result.prompt_version and result.prompt_version.startswith("echo/v1@")
    assert result.model == "fake"
    assert events.types() == [
        "agent_started",
        "llm_called",
        "tool_called",
        "llm_called",
        "agent_finished",
    ]
    assert len(audit.records) == 1 and audit.records[0].investigation_id == "inv-1"

    # Prompt = common preamble + rendered agent prompt; tools = allowlisted + submit.
    first = llm.requests[0]
    assert first["messages"][0].content.startswith("COMMON RULES")
    assert "Echo agent for payment-service in production." in first["messages"][0].content
    tool_names = {t.name for t in first["tools"]}
    assert tool_names == {"search_logs", "list_indices", "submit"}
    assert first["tool_choice"] == "required"


async def test_unknown_evidence_id_is_rejected_then_corrected(tmp_path: Path) -> None:
    def responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
        evidence_id = last_evidence_id(messages)
        if evidence_id is None:
            return tool_call("search_logs", {"service": "payment-service"})
        rejected = any(m.role == "tool" and "Rejected" in (m.content or "") for m in messages)
        return submit(evidence_id if rejected else "ev-invented")

    deps, _, _ = make_deps(tmp_path, FakeLLMProvider(responder=responder))
    result = await EchoAgent(deps).run(make_task())
    assert result.status is AgentStatus.SUCCESS
    assert result.usage.calls == 3


async def test_fact_without_evidence_is_rejected(tmp_path: Path) -> None:
    script = [submit(None), submit(None, findings=[], status="no_signal", signals=[])]
    deps, _, _ = make_deps(tmp_path, FakeLLMProvider(script))
    result = await EchoAgent(deps).run(make_task())
    assert result.status is AgentStatus.NO_SIGNAL
    assert result.usage.calls == 2


async def test_blocked_tool_is_reported_to_llm(tmp_path: Path) -> None:
    script = [
        tool_call("delete_index", {"name": "x"}),
        submit(None, findings=[], status="no_signal"),
    ]
    llm = FakeLLMProvider(script)
    deps, _, audit = make_deps(tmp_path, llm)
    result = await EchoAgent(deps).run(make_task())
    assert result.tool_calls[0].status == "blocked"
    assert audit.records[0].tool_call.status == "blocked"
    tool_message = llm.requests[1]["messages"][-1]
    assert "not allowed" in (tool_message.content or "")


async def test_tool_call_limit(tmp_path: Path) -> None:
    script = [
        tool_call("search_logs", {"service": "a"}, "c1"),
        tool_call("search_logs", {"service": "b"}, "c2"),
        submit(None, findings=[], status="no_signal"),
    ]
    llm = FakeLLMProvider(script)
    deps, _, _ = make_deps(tmp_path, llm, settings=make_settings(max_tool_calls=1))
    result = await EchoAgent(deps).run(make_task())
    assert len(result.tool_calls) == 1
    assert "max_tool_calls" in (llm.requests[2]["messages"][-1].content or "")


async def test_step_limit_returns_partial_with_evidence(tmp_path: Path) -> None:
    def responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
        return tool_call("search_logs", {"service": "payment-service"})

    deps, _, _ = make_deps(
        tmp_path, FakeLLMProvider(responder=responder), settings=make_settings(max_steps=2)
    )
    result = await EchoAgent(deps).run(make_task())
    assert result.status is AgentStatus.PARTIAL
    assert "Step limit" in result.summary
    assert len(result.evidence) == 2


async def test_no_tool_call_gets_nudged(tmp_path: Path) -> None:
    script = [text("I think it is fine"), submit(None, findings=[], status="no_signal")]
    llm = FakeLLMProvider(script)
    deps, _, _ = make_deps(tmp_path, llm)
    result = await EchoAgent(deps).run(make_task())
    assert result.status is AgentStatus.NO_SIGNAL
    assert "Call a tool" in (llm.requests[1]["messages"][-1].content or "")


async def test_execution_timeout_is_partial_or_failed(tmp_path: Path) -> None:
    llm = FakeLLMProvider(responder=search_then_submit)
    deps, _, _ = make_deps(
        tmp_path, llm, server=make_server(delay_s=5), settings=make_settings(max_execution_s=0.3)
    )
    result = await EchoAgent(deps).run(make_task())
    assert result.status in (AgentStatus.FAILED, AgentStatus.PARTIAL)
    assert "time limit" in result.summary or result.tool_calls[0].status == "timeout"


async def test_token_budget(tmp_path: Path) -> None:
    llm = FakeLLMProvider(responder=search_then_submit)
    deps, _, _ = make_deps(tmp_path, llm, settings=make_settings(max_tokens=10))
    result = await EchoAgent(deps).run(make_task())
    assert result.status is AgentStatus.PARTIAL
    assert "Token budget" in result.summary


async def test_unreachable_mcp_server_fails_cleanly(tmp_path: Path) -> None:
    settings = make_settings()
    registry = MCPRegistry(settings, overrides={"logs": "http://127.0.0.1:9/mcp"})
    deps, events, _ = make_deps(tmp_path, FakeLLMProvider(), settings=settings, mcp=registry)
    deps.mcp = registry
    result = await EchoAgent(deps).run(make_task())
    assert result.status is AgentStatus.FAILED
    assert "Data source unavailable" in result.summary
    assert events.types()[-1] == "agent_finished"


async def test_record_then_replay_is_deterministic(tmp_path: Path) -> None:
    settings = make_settings()
    fixtures = tmp_path / "fixtures"
    recording = MCPRegistry(settings, overrides={"logs": make_server()}, record_dir=fixtures)
    deps, _, _ = make_deps(tmp_path, FakeLLMProvider(responder=search_then_submit), mcp=recording)
    live = await EchoAgent(deps).run(make_task())
    assert live.status is AgentStatus.SUCCESS, live.summary
    assert (fixtures / "logs.json").is_file()

    replaying = MCPRegistry(settings, replay_dir=fixtures)
    deps2, _, _ = make_deps(
        tmp_path / "2", FakeLLMProvider(responder=search_then_submit), mcp=replaying
    )
    replayed = await EchoAgent(deps2).run(make_task())
    assert replayed.status is live.status is AgentStatus.SUCCESS
    assert replayed.evidence[0].data == live.evidence[0].data


def test_registry() -> None:
    registry = AgentRegistry()
    registry.register(EchoAgent)
    registry.register(EchoAgent)  # idempotent for the same class
    assert registry.names() == ["echo"]
    assert registry.specs()[0].capabilities == ["logs"]
