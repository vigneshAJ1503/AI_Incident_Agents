"""Incident scenarios with ground truth: scenarios/<id>/expected.yaml."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aiops.core.config import ConfigError, format_validation_error, load_yaml
from aiops.core.models import AgentStatus


class AgentExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: list[AgentStatus] = Field(min_length=1)
    signals: list[str] = Field(default_factory=list)  # all must be present
    forbidden_signals: list[str] = Field(default_factory=list)
    must_mention: list[str] = Field(default_factory=list)  # case-insensitive, summary+findings
    min_evidence: int = 0


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


def load_scenario(path: Path) -> Scenario:
    file = path / "expected.yaml" if path.is_dir() else path
    try:
        return Scenario.model_validate(load_yaml(file))
    except ValidationError as err:
        raise ConfigError(format_validation_error(err, file)) from err


def load_scenarios(root: Path) -> list[Scenario]:
    return [load_scenario(p) for p in sorted(root.glob("*/expected.yaml"))]
