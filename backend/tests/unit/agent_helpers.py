"""Shared helpers for agent tests: in-process MCP server, deps, fake-LLM responders."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from aiops.agents.base import AgentDeps, AgentSpec, BaseAgent
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import AgentLimits, Settings, load_settings
from aiops.core.events import MemoryEventSink
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.core.models import AgentTask, EvidenceKind, IncidentContext, TimeRange
from aiops.core.prompts import PromptLoader
from aiops.llm.base import ChatMessage, LLMProvider, LLMResponse, ToolSpec
from aiops.llm.fake import tool_call
from aiops.mcp.registry import MCPRegistry

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
EVIDENCE_ID = re.compile(r"evidence_id: (ev-[0-9a-f]+)")


class EchoAgent(BaseAgent):
    spec = AgentSpec(
        name="echo",
        version="1",
        description="Test agent",
        capabilities=["logs"],
        evidence_kind=EvidenceKind.LOG,
        prompt="echo",
    )


def make_server(delay_s: float = 0.0) -> MCPServer:
    server = MCPServer("fake-logs")

    @server.tool()
    async def search_logs(service: str) -> dict[str, Any]:
        """Search logs."""
        if delay_s:
            await asyncio.sleep(delay_s)
        return {"service": service, "errors": 427, "top": "Database connection timeout"}

    @server.tool()
    def list_indices() -> dict[str, Any]:
        """List indices."""
        return {"indices": ["payment-prod-2026.09.25"]}

    return server


def make_prompts(tmp_path: Path) -> PromptLoader:
    (tmp_path / "common").mkdir(parents=True)
    (tmp_path / "common" / "v1.md").write_text("COMMON RULES")
    (tmp_path / "echo").mkdir()
    (tmp_path / "echo" / "v1.md").write_text("Echo agent for $service in $environment.")
    return PromptLoader(tmp_path)


def make_settings(**limits: Any) -> Settings:
    settings = load_settings("local", REPO_CONFIG)
    return settings.model_copy(update={"limits": AgentLimits(**limits)}) if limits else settings


def make_deps(
    tmp_path: Path,
    llm: LLMProvider,
    *,
    server: MCPServer | None = None,
    settings: Settings | None = None,
    mcp: MCPRegistry | None = None,
) -> tuple[AgentDeps, MemoryEventSink, MemoryAuditSink]:
    settings = settings or make_settings()
    audit = MemoryAuditSink()
    events = MemoryEventSink()
    registry = mcp or MCPRegistry(
        settings, audit=audit, overrides={"logs": server or make_server()}
    )
    deps = AgentDeps(
        settings=settings,
        llm=llm,
        mcp=registry,
        prompts=make_prompts(tmp_path / "prompts"),
        catalog=ServiceCatalog.from_settings(settings),
        events=events,
    )
    return deps, events, audit


def make_task(question: str = "Payment API returning 500") -> AgentTask:
    context = IncidentContext(
        question=question,
        service="payment-service",
        environment="production",
        time_range=TimeRange.last("30m", now=NOW),
    )
    return AgentTask(agent="echo", objective=question, context=context, investigation_id="inv-1")


def last_evidence_id(messages: list[ChatMessage]) -> str | None:
    for message in reversed(messages):
        if message.role == "tool" and message.content:
            match = EVIDENCE_ID.search(message.content)
            if match:
                return match.group(1)
    return None


def submit(evidence_id: str | None, **overrides: Any) -> LLMResponse:
    report: dict[str, Any] = {
        "status": "success",
        "summary": "427 database connection timeouts in the window.",
        "findings": [
            {
                "kind": "FACT",
                "type": "error_pattern",
                "description": "Database connection timeout errors",
                "evidence_ids": [evidence_id] if evidence_id else [],
            }
        ],
        "signals": ["db_timeout_errors_up"],
        "confidence": 0.9,
    }
    report.update(overrides)
    return tool_call("submit", report)


def search_then_submit(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
    """Responder: first call search_logs, then submit citing the returned evidence id."""
    evidence_id = last_evidence_id(messages)
    if evidence_id is None:
        return tool_call("search_logs", {"service": "payment-service"})
    return submit(evidence_id)
