"""Re-record Alert agent MCP fixtures from the live local stack.

    make alertmanager-up    # + alertmanager-mcp (docker-compose.mcp.yml)
    cd backend && uv run python -m tests.fixtures.record_alerts

Seeds each scenario's alerts with startsAt anchored at the FIXED time, so fixtures
(which match on exact tool arguments) are reproducible and "firing since" matches
the incident timeline. Leaves Alertmanager cleared (S0) afterwards.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from aiops.agents.alert_agent import AlertAgent
from aiops.agents.deps import build_deps
from aiops.core.config import load_settings
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from aiops.seed.alertmanager import seed_scenario_alerts
from tests.fixtures.scenario_context import FIXED_NOW, task_for

HERE = Path(__file__).parent
SCENARIOS = ["S0", "S1", "S2", "S3", "S4", "S5"]
AM_URL = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093")


def responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
    """Minimal submit: recording captures the deterministic tool exchanges only."""
    return tool_call("submit", {"status": "no_signal", "summary": "recording", "confidence": 0.1})


async def record(scenario: str) -> None:
    seed_scenario_alerts(AM_URL, scenario, FIXED_NOW)
    settings = load_settings("local")
    deps = build_deps(
        settings, llm=FakeLLMProvider(responder=responder), record_dir=HERE / "alerts" / scenario
    )
    result = await AlertAgent(deps).run(task_for(scenario, "alerts"))
    print(scenario, result.status.value, result.signals, len(result.evidence), "evidence")


if __name__ == "__main__":
    for s in SCENARIOS:
        asyncio.run(record(s))
    seed_scenario_alerts(AM_URL, "S0", FIXED_NOW)
