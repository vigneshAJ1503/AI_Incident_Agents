"""Rule-based scoring of an agent result against a scenario expectation."""

from __future__ import annotations

from pydantic import BaseModel

from aiops.core.models import AgentResult
from aiops.evals.scenario import AgentExpectation


class Check(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class ScoreCard(BaseModel):
    scenario: str
    agent: str
    checks: list[Check]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def score(self) -> float:
        return sum(c.passed for c in self.checks) / len(self.checks) if self.checks else 1.0


def score_agent(scenario_id: str, result: AgentResult, expected: AgentExpectation) -> ScoreCard:
    text = " ".join([result.summary, *(f.description for f in result.findings)]).casefold()
    signals = set(result.signals)
    checks = [
        Check(
            name="status",
            passed=result.status in expected.status,
            detail=f"got {result.status.value}, expected one of {[s.value for s in expected.status]}",
        ),
        Check(
            name="min_evidence",
            passed=len(result.evidence) >= expected.min_evidence,
            detail=f"{len(result.evidence)} evidence items (min {expected.min_evidence})",
        ),
    ]
    for signal in expected.signals:
        checks.append(Check(name=f"signal:{signal}", passed=signal in signals))
    for signal in expected.forbidden_signals:
        checks.append(Check(name=f"no_signal:{signal}", passed=signal not in signals))
    for phrase in expected.must_mention:
        checks.append(Check(name=f"mentions:{phrase}", passed=phrase.casefold() in text))
    return ScoreCard(scenario=scenario_id, agent=result.agent, checks=checks)
