"""Re-record Log agent MCP fixtures from the live local stack.

    make infra-up mcp-up
    cd backend && uv run python -m tests.fixtures.record_logs

Seeds each scenario at a FIXED time so fixtures (which match on exact tool
arguments, including start/end) are reproducible.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path

from aiops.agents.deps import build_deps
from aiops.agents.log_agent import LogAgent
from aiops.core.config import load_settings
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from aiops.seed.elasticsearch import ElasticsearchSeeder
from aiops.seed.logs import LogGenerator, SeedWindow, index_name
from tests.fixtures.scenario_context import FIXED_NOW, task_for

HERE = Path(__file__).parent
SCENARIOS = ["S0", "S1"]


def seed(scenario: str) -> None:
    window = SeedWindow.build(FIXED_NOW, hours=26)
    seeder = ElasticsearchSeeder(os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200"))
    try:
        seeder.ensure_template()
        seeder.delete_seeded_indices()
        docs = (
            (index_name(d["service"], "production", datetime.fromisoformat(d["@timestamp"])), d)
            for d in LogGenerator(scenario, window).generate()
        )
        seeder.write_meta(scenario, window, 42, seeder.bulk(docs))
    finally:
        seeder.close()


def responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
    """One follow-up sample query, then a minimal submit (content irrelevant for recording)."""
    if not any(m.role == "tool" for m in messages):
        return tool_call(
            "search_logs",
            {
                "index": "payment-prod-*",
                "start": "2026-09-25T10:00:00Z",
                "end": "2026-09-25T10:30:00Z",
                "levels": ["ERROR"],
                "size": 5,
            },
        )
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
