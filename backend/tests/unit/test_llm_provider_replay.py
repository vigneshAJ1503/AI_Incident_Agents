"""A full orchestrated replay investigation on each enterprise provider (PR-P4b).

The seven specialist agents, the planner and the RCA agent run unchanged on recorded
MCP fixtures; only the LLM differs: the real ``anthropic`` / ``bedrock`` /
``azure_openai`` adapter + vendor SDK, answered by a fake transport that scripts the
tool calls (``submit`` of the deterministic overview; the RCA ranking). Proves that
switching the LLM platform is profile configuration only. Zero tokens, no network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from aiops.agents.deps import AgentDeps, build_deps
from aiops.core.config import LLMConfig, Settings, load_settings
from aiops.core.events import EventBus, EventSink
from aiops.core.models import AgentStatus, InvestigationStatus
from aiops.evals.replay import echo_responder
from aiops.llm.base import ChatMessage, LLMProvider, LLMResponse, ToolSpec
from aiops.llm.fake import tool_call
from aiops.orchestrator.engine import InvestigationRequest, Orchestrator
from aiops.orchestrator.replay import ReplaySource, load_replay
from tests.unit import llm_wire as w

CONFIG = Path(__file__).resolve().parents[3] / "config"
AGENTS = {"logs", "metrics", "alerts", "k8s", "code", "knowledge", "tickets"}
CANDIDATES = "Candidates (deterministic, confidence already calibrated):\n"


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings("local", CONFIG)


def scripted_model() -> Callable[[list[ChatMessage], list[ToolSpec] | None], LLMResponse]:
    """Specialists: submit the deterministic overview (the replay-eval script). RCA: rank
    the candidates in the given order, phrasing unchanged."""
    echo = echo_responder()

    def respond(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
        submit = next((t for t in tools or [] if t.name == "submit"), None)
        if submit is not None and "ranked" in submit.parameters.get("properties", {}):
            user = next(m.content or "" for m in messages if m.role == "user")
            candidates = json.loads(user.split(CANDIDATES, 1)[1])
            ranked = [{"id": c["id"], "statement": c["statement"]} for c in candidates]
            return tool_call("submit", {"ranked": ranked}, call_id="rank-1")
        return echo(messages, tools)

    return respond


class ProviderReplay(ReplaySource):
    """Recorded MCP fixtures, but agents talk to ``llm`` instead of the fake."""

    llm: LLMProvider

    def deps(
        self, settings: Settings, agent: str, service: str | None, events: EventSink
    ) -> AgentDeps:
        return build_deps(
            settings,
            llm=self.llm,
            replay_dir=self.fixture_dir(agent, service),
            replay_lenient=True,
            events=events,
        )


def build(name: str, respond: Any) -> tuple[LLMProvider, w.Recorder, LLMConfig]:
    if name == "anthropic":
        provider, rec = w.anthropic_server(respond)
        return provider, rec, w.anthropic_config()
    if name == "azure_openai":
        provider, rec = w.azure_server(respond)
        return provider, rec, w.azure_config()
    provider, rec = w.bedrock_server(respond)
    return provider, rec, w.bedrock_config()


@pytest.mark.parametrize("provider_name", ["anthropic", "bedrock", "azure_openai"])
def test_full_replay_investigation_on_provider(settings: Settings, provider_name: str) -> None:
    provider, rec, llm_config = build(provider_name, scripted_model())
    profile = settings.model_copy(update={"llm": llm_config})
    source = load_replay(profile, "S1")
    replay = ProviderReplay(
        scenario=source.scenario, fixtures=source.fixtures, anchor_end=source.anchor_end
    )
    replay.llm = provider
    bus = EventBus()
    orch = Orchestrator(profile, llm=provider, replay=replay, bus=bus)
    assert not orch.deterministic_only

    inv = asyncio.run(orch.investigate(InvestigationRequest(question="", mode="replay")))

    assert inv.status is InvestigationStatus.COMPLETED, [r.error for r in inv.results]
    assert {r.agent for r in inv.results} == AGENTS
    assert all(r.status is not AgentStatus.FAILED for r in inv.results)
    # Same ground truth as the zero-token replay (test_rca_response.py)
    top = inv.hypotheses[0]
    assert "pool" in top.statement and "v1.8.2" in top.statement
    assert inv.report is not None and inv.report.confidence >= 0.8
    # Every LLM call went through the vendor wire protocol and was accounted for.
    calls = len(rec.requests)
    assert calls >= len(AGENTS) + 1  # each specialist + the RCA ranking
    assert inv.usage.calls == calls
    assert inv.usage.total_tokens == calls * sum(w.USAGE)
    models = [b.get("model") or "" for b in rec.bodies] + [r["url"] for r in rec.requests]
    assert any("rca-model" in m for m in models)  # the rca role's model/deployment
    assert any("ranked" in json.dumps(b) for b in rec.bodies)
