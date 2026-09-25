"""Fixed-time task context shared by fixture recording and replay tests.

Questions/services come from the scenarios' ground truth (scenarios/*/expected.yaml).
"""

from __future__ import annotations

from pathlib import Path

from aiops.core.models import AgentTask
from aiops.evals.replay import REPLAY_NOW
from aiops.evals.scenario import Scenario, load_scenarios

FIXED_NOW = REPLAY_NOW
SCENARIOS_DIR = Path(__file__).resolve().parents[3] / "scenarios"
SCENARIOS: dict[str, Scenario] = {s.id: s for s in load_scenarios(SCENARIOS_DIR)}


def task_for(scenario_id: str, agent: str = "logs") -> AgentTask:
    return SCENARIOS[scenario_id].task(agent, FIXED_NOW)
