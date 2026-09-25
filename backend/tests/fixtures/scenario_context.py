"""Fixed-time task context shared by fixture recording and replay tests.

Questions/services come from the scenarios' ground truth (scenarios/*/expected.yaml).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from aiops.core.models import AgentTask, IncidentContext, TimeRange
from aiops.evals.scenario import Scenario, load_scenarios

FIXED_NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
SCENARIOS_DIR = Path(__file__).resolve().parents[3] / "scenarios"
SCENARIOS: dict[str, Scenario] = {s.id: s for s in load_scenarios(SCENARIOS_DIR)}


def task_for(scenario_id: str) -> AgentTask:
    scenario = SCENARIOS[scenario_id]
    context = IncidentContext(
        question=scenario.question,
        service=scenario.service,
        environment=scenario.environment,
        time_range=TimeRange.last(scenario.window, now=FIXED_NOW),
    )
    return AgentTask(agent="logs", objective=scenario.question, context=context)
