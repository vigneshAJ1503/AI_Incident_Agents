"""Requires Minikube (make k8s-up) and kubernetes-mcp (make kubernetes-mcp-up).

Runs the K8s agent against the live cluster through the real MCP server. Other
sessions may have a fault injected, so this checks the contract (calls, evidence,
vocabulary, no false 'healthy' on partial data), not a particular scenario; the
scenario ground truth is covered by the live-recorded replay fixtures.
Run with: make test-integration
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiops.agents.deps import build_deps
from aiops.agents.k8s_agent import K8sAgent
from aiops.agents.k8s_agent.analysis import SIGNALS
from aiops.core.config import load_settings
from aiops.core.models import AgentStatus
from aiops.evals.replay import echo_responder
from aiops.llm.fake import FakeLLMProvider
from tests.fixtures.scenario_context import SCENARIOS

pytestmark = pytest.mark.integration
CONFIG = Path(__file__).resolve().parents[3] / "config"


@pytest.mark.parametrize("service", ["payment-service", "order-service", "user-service"])
def test_k8s_agent_live(service: str) -> None:
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=FakeLLMProvider(responder=echo_responder()))
    task = SCENARIOS["S0"].task("k8s", datetime.now(UTC))
    task = task.model_copy(update={"context": task.context.model_copy(update={"service": service})})
    result = asyncio.run(K8sAgent(deps).run(task))
    assert result.status in (AgentStatus.SUCCESS, AgentStatus.NO_SIGNAL), result.error
    assert [c.tool for c in result.tool_calls][:3] == ["get_deployment", "list_pods", "list_events"]
    assert all(c.status == "ok" for c in result.tool_calls)
    assert len(result.evidence) == 4  # deployment, pods, events, dependencies
    assert set(result.signals) <= set(SIGNALS) and result.signals
    assert f"{service}: " in result.summary  # the data-derived headline
