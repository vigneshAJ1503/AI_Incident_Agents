"""Tickets agent against the live mock-tickets-mcp (make infra-up mock-tickets-up).

Seeds the backlog at FIXED_NOW (replaces the OPS/WEB tickets in schema `tickets`).
"""

from __future__ import annotations

import pytest

from aiops.agents.deps import build_deps
from aiops.agents.tickets_agent import TicketsAgent
from aiops.core.config import load_settings
from aiops.core.models import AgentStatus
from aiops.evals.replay import echo_responder
from aiops.evals.scoring import score_agent
from aiops.llm.fake import FakeLLMProvider
from aiops.seed.tickets import PostgresTicketSeeder, default_dsn
from tests.conftest import REPO_ROOT
from tests.fixtures.scenario_context import FIXED_NOW, SCENARIOS, task_for

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _seeded() -> None:
    PostgresTicketSeeder(default_dsn()).seed(FIXED_NOW)


@pytest.mark.parametrize("scenario", ["S0", "S1", "S3", "S5"])
async def test_live_scenarios(scenario: str) -> None:
    settings = load_settings("local", REPO_ROOT / "config")
    deps = build_deps(settings, llm=FakeLLMProvider(responder=echo_responder()))
    result = await TicketsAgent(deps).run(task_for(scenario, "tickets"))
    card = score_agent(scenario, result, SCENARIOS[scenario].agents["tickets"])
    assert card.passed, [c for c in card.checks if not c.passed]
    if scenario == "S1":
        assert result.status is AgentStatus.SUCCESS
        assert any(e.data.get("key") == "OPS-12" for e in result.evidence)
