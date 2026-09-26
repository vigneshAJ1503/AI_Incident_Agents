"""Incident scenarios with ground truth: scenarios/<id>/expected.yaml."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aiops.core.config import ConfigError, format_validation_error, load_yaml
from aiops.core.models import AgentStatus, AgentTask, IncidentContext, TimeRange


class AgentExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: list[AgentStatus] = Field(min_length=1)
    signals: list[str] = Field(default_factory=list)  # all must be present
    forbidden_signals: list[str] = Field(default_factory=list)
    must_mention: list[str] = Field(default_factory=list)  # case-insensitive, summary+findings
    min_evidence: int = 0
    # Task input for this agent when evaluated standalone, e.g. representative upstream
    # findings: {"signals": [...], "patterns": [...]}. The orchestrator passes real ones.
    hints: dict[str, Any] = Field(default_factory=dict)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str = ""
    question: str
    service: str | None = None
    environment: str | None = "production"
    window: str = "30m"
    root_cause: str | None = None  # None = healthy / no incident
    agents: dict[str, AgentExpectation] = Field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        """No ground-truth root cause: any reported anomaly is a false positive."""
        return self.root_cause is None

    def task(self, agent: str, now: datetime, time_range: TimeRange | None = None) -> AgentTask:
        """The agent task an engineer's question produces, with the window ending at ``now``
        (or exactly ``time_range``, e.g. the window a live fixture was recorded with)."""
        context = IncidentContext(
            question=self.question,
            service=self.service,
            environment=self.environment,
            time_range=time_range or TimeRange.last(self.window, now=now),
        )
        expectation = self.agents.get(agent)
        hints = dict(expectation.hints) if expectation else {}
        return AgentTask(agent=agent, objective=self.question, context=context, hints=hints)


def load_scenario(path: Path) -> Scenario:
    """Load ``expected.yaml`` plus per-agent files ``agents/<agent>.yaml`` (one file per agent)."""
    file = path / "expected.yaml" if path.is_dir() else path
    data = load_yaml(file)
    agents = dict(data.get("agents") or {})
    for agent_file in sorted((file.parent / "agents").glob("*.yaml")):
        if agent_file.stem in agents:
            raise ConfigError(
                f"Agent '{agent_file.stem}' is defined twice for scenario in {file.parent}"
            )
        agents[agent_file.stem] = load_yaml(agent_file)
    data["agents"] = agents
    try:
        return Scenario.model_validate(data)
    except ValidationError as err:
        raise ConfigError(format_validation_error(err, file)) from err


def load_scenarios(root: Path) -> list[Scenario]:
    return [load_scenario(p) for p in sorted(root.glob("*/expected.yaml"))]
