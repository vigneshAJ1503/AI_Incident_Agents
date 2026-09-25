"""Re-record Tickets agent MCP fixtures from the live mock-tickets-mcp.

    make infra-up mock-tickets-up
    cd backend && uv run python -m tests.fixtures.record_tickets

Seeds the ticket backlog at FIXED_NOW (fixtures match on exact tool arguments, which
contain absolute dates derived from the task window), then records S0-S5 plus variants.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiops.agents.deps import build_deps
from aiops.agents.tickets_agent import TicketsAgent
from aiops.core.config import load_settings
from aiops.core.models import AgentTask
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from aiops.seed.tickets import PostgresTicketSeeder, default_dsn
from tests.fixtures.scenario_context import FIXED_NOW, task_for

HERE = Path(__file__).parent
SCENARIOS = ["S0", "S1", "S2", "S3", "S4", "S5"]


def variants() -> dict[str, AgentTask]:
    """Extra recordings: the same scenario with symptoms from earlier findings (round 2)."""
    s2 = task_for("S2", "tickets")
    s2 = s2.model_copy(
        update={"context": s2.context.model_copy(update={"symptoms": ["oom_errors"]})}
    )
    return {"S2-oom-symptoms": s2}


def responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
    """Minimal submit: recording captures the deterministic tool exchanges only."""
    return tool_call("submit", {"status": "no_signal", "summary": "recording", "confidence": 0.1})


async def record(name: str, task: AgentTask) -> None:
    settings = load_settings("local")
    deps = build_deps(
        settings, llm=FakeLLMProvider(responder=responder), record_dir=HERE / "tickets" / name
    )
    result = await TicketsAgent(deps).run(task)
    print(name, result.status.value, result.signals, len(result.evidence), "evidence")


async def main() -> None:
    PostgresTicketSeeder(default_dsn()).seed(FIXED_NOW)
    for scenario in SCENARIOS:
        await record(scenario, task_for(scenario, "tickets"))
    for name, task in variants().items():
        await record(name, task)


if __name__ == "__main__":
    asyncio.run(main())
