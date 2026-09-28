"""A full investigation whose data source leaks every kind of secret (PR-042).

Used by the SQLite unit test and the Postgres integration test: the secrets must not
appear in what the LLM sees, the audit log, the stored investigation/events, the SSE
stream or any API response.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from aiops.agents.base import AgentDeps, AgentSpec, BaseAgent
from aiops.agents.registry import AgentRegistry
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import OrchestratorConfig, Settings
from aiops.core.events import EventBus, EventSink
from aiops.core.guardrails.audit import AuditSink
from aiops.core.models import EvidenceKind
from aiops.llm.fake import FakeLLMProvider
from aiops.mcp.registry import MCPRegistry
from aiops.orchestrator.engine import Orchestrator
from tests.security.payloads import leaky_log_line, secrets
from tests.unit.agent_helpers import make_prompts, search_then_submit


class LeakyLogsAgent(BaseAgent):
    spec = AgentSpec(
        name="logs",
        version="1",
        description="Echo agent bound to the logs capability",
        capabilities=["logs"],
        evidence_kind=EvidenceKind.LOG,
        prompt="echo",
    )


def leaky_server() -> MCPServer:
    server = MCPServer("leaky-logs")

    @server.tool()
    def search_logs(service: str) -> dict[str, Any]:
        """Search logs."""
        return {
            "service": service,
            "errors": 12,
            "hits": [{"message": leaky_log_line()}],
            "config": {"db_password": secrets()["db_password"], "client_secret": "s3cr3t-v4lue"},
        }

    @server.tool()
    def list_indices() -> dict[str, Any]:
        """List indices."""
        return {"indices": ["payment-prod-2026.09.25"]}

    return server


def leaky_orchestrator_factory(tmp_path: Path, llm: FakeLLMProvider, audit: AuditSink) -> Any:
    def factory(settings: Settings, bus: EventBus, scenario: str | None) -> Orchestrator:
        tuned = settings.model_copy(
            update={
                "orchestrator": OrchestratorConfig(
                    max_rounds=1, llm_rca=False, llm_planner_fallback=False
                )
            }
        )
        registry = AgentRegistry()
        registry.register(LeakyLogsAgent)
        prompts = make_prompts(tmp_path / "prompts")

        def deps(agent: str, service: str | None, events: EventSink) -> AgentDeps:
            return AgentDeps(
                settings=tuned,
                llm=llm,
                mcp=MCPRegistry(tuned, audit=audit, overrides={"logs": leaky_server()}),
                prompts=prompts,
                catalog=ServiceCatalog.from_settings(tuned),
                events=events,
            )

        return Orchestrator(tuned, llm=llm, bus=bus, registry=registry, deps_factory=deps)

    return factory


def leaky_llm() -> FakeLLMProvider:
    return FakeLLMProvider(responder=search_then_submit)


def assert_no_secrets(where: str, blob: Any) -> None:
    text = blob if isinstance(blob, str) else json.dumps(blob, default=str)
    leaked = [name for name, value in secrets().items() if value in text]
    leaked += ["client_secret"] if "s3cr3t-v4lue" in text else []
    assert not leaked, f"raw secrets in {where}: {leaked}"
