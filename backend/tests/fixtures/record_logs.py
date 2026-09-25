"""Re-record Log agent MCP fixtures from the live local stack.

    make infra-up mcp-up
    cd backend && uv run python -m tests.fixtures.record_logs

Seeds each scenario at a FIXED time so fixtures (which match on exact tool
arguments, including start/end) are reproducible.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from aiops.agents.deps import build_deps
from aiops.agents.log_agent import LogAgent
from aiops.core.config import load_settings
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from aiops.seed.elasticsearch import seed_scenario_logs
from tests.fixtures.scenario_context import FIXED_NOW, task_for

HERE = Path(__file__).parent
SCENARIOS = ["S0", "S1", "S2", "S3", "S4", "S5"]


def seed(scenario: str) -> None:
    url = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")
    seed_scenario_logs(url, scenario, FIXED_NOW)


def responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
    """Minimal submit: recording captures the deterministic tool exchanges only."""
    return tool_call("submit", {"status": "no_signal", "summary": "recording", "confidence": 0.1})


async def record(scenario: str) -> None:
    seed(scenario)
    settings = load_settings("local")
    deps = build_deps(
        settings, llm=FakeLLMProvider(responder=responder), record_dir=HERE / "logs" / scenario
    )
    result = await LogAgent(deps).run(task_for(scenario))
    print(scenario, result.status.value, len(result.evidence), "evidence")


if __name__ == "__main__":
    for s in SCENARIOS:
        asyncio.run(record(s))
