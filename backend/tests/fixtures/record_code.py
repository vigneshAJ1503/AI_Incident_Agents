"""Re-record Code agent MCP fixtures from the live local git-mcp server.

    make seed-repo S=S1 && docker compose ... up -d --build git-mcp   # see make record-code
    cd backend && uv run python -m tests.fixtures.record_code

Builds each scenario's sample repo at the FIXED time (same SHAs every time) into
.data/sample-repo, which git-mcp mounts read-only, then records the agent's tool
exchanges. Fixtures match on exact tool arguments (window, SHAs).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiops.agents.code_agent import CodeAgent
from aiops.agents.deps import build_deps
from aiops.core.config import load_settings
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from aiops.seed.git_repo import build_sample_repo, default_repo_path
from tests.fixtures.scenario_context import FIXED_NOW, task_for

HERE = Path(__file__).parent
SCENARIOS = ["S0", "S1", "S2", "S3", "S4", "S5"]


def responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
    """Minimal submit: recording captures the deterministic tool exchanges only."""
    return tool_call("submit", {"status": "no_signal", "summary": "recording", "confidence": 0.1})


async def record(scenario: str) -> None:
    build_sample_repo(default_repo_path(), scenario, FIXED_NOW)
    settings = load_settings("local")
    deps = build_deps(
        settings, llm=FakeLLMProvider(responder=responder), record_dir=HERE / "code" / scenario
    )
    result = await CodeAgent(deps).run(task_for(scenario, "code"))
    print(scenario, result.status.value, result.signals, len(result.evidence), "evidence")


if __name__ == "__main__":
    for s in SCENARIOS:
        asyncio.run(record(s))
