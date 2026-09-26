"""Requires Prometheus + prometheus-mcp (`make infra-up k8s-up prometheus-mcp-up`).

Runs the Metrics agent against live Prometheus through the real MCP server (read-only).
A fault may be injected by another session, so this checks the contract (10 range
queries, evidence shape, signal vocabulary), not a scenario; the scenario ground truth is
covered by the live-recorded replay fixtures (tests/unit/test_metrics_agent.py).
Run with: make test-integration
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiops.agents.deps import build_deps
from aiops.agents.metrics_agent import MetricsAgent
from aiops.agents.metrics_agent.analysis import SIGNALS
from aiops.core.config import load_settings
from aiops.core.models import AgentStatus
from aiops.evals.replay import echo_responder
from aiops.llm.fake import FakeLLMProvider
from tests.fixtures.scenario_context import SCENARIOS

pytestmark = pytest.mark.integration
CONFIG = Path(__file__).resolve().parents[3] / "config"


@pytest.mark.parametrize("service", ["payment-service", "order-service"])
def test_metrics_agent_live(service: str) -> None:
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=FakeLLMProvider(responder=echo_responder()))
    task = SCENARIOS["S0"].task("metrics", datetime.now(UTC))
    task = task.model_copy(update={"context": task.context.model_copy(update={"service": service})})
    result = asyncio.run(MetricsAgent(deps).run(task))
    assert result.status in (AgentStatus.SUCCESS, AgentStatus.NO_SIGNAL), result.error
    assert [c.tool for c in result.tool_calls] == ["query_range"] * 10
    assert all(c.status == "ok" for c in result.tool_calls)
    assert len(result.evidence) == 10
    assert result.signals and set(result.signals) <= set(SIGNALS)
    errors = next(e for e in result.evidence if e.data.get("metric") == "error_rate")
    assert service in errors.data["services"] and errors.data["series"][service]
    assert errors.link
