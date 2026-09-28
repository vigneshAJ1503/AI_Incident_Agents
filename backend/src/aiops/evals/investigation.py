"""Whole-investigation evals (PR-034): replay S0-S5 end to end and score the RCA.

Ground truth per scenario (``expected.yaml``):

* ``root_cause`` (text) and ``investigation.root_cause_keywords``: groups of
  alternatives; the top hypothesis must mention one word of **every** group;
* ``investigation.min_confidence`` for the root cause;
* healthy scenarios (``root_cause: null``) must end with "no incident": no hypothesis,
  confidence 0, severity ``none``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pydantic import BaseModel, Field

from aiops.core.config import Settings
from aiops.core.events import INVESTIGATION_EVENT_TYPES, EventBus
from aiops.core.models import Investigation, InvestigationStatus
from aiops.evals.runner import EvalPaths
from aiops.evals.scenario import Scenario, load_scenarios
from aiops.evals.scoring import Check
from aiops.orchestrator.engine import InvestigationRequest, Orchestrator
from aiops.orchestrator.replay import ReplaySource


class InvestigationEval(BaseModel):
    scenario: str
    title: str
    investigation_id: str
    status: str
    root_cause: str | None
    confidence: float
    severity: str
    checks: list[Check] = Field(default_factory=list)
    tokens: int = 0
    duration_ms: float = 0.0

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)


def score_investigation(
    scenario: Scenario, inv: Investigation, events: Sequence[str]
) -> list[Check]:
    report = inv.report
    expected = scenario.investigation
    checks = [
        Check(
            name="completed",
            passed=inv.status is InvestigationStatus.COMPLETED,
            detail=f"status {inv.status.value}",
        ),
        Check(
            name="contract_events",
            passed=set(events) <= set(INVESTIGATION_EVENT_TYPES) and "report_ready" in events,
            detail=f"{len(events)} events",
        ),
        Check(name="report", passed=report is not None and bool(report.markdown)),
    ]
    if report is None:
        return checks
    top = next((h for h in inv.hypotheses if h.id == report.root_cause_hypothesis_id), None)
    if expected.severity is not None:
        checks.append(
            Check(
                name=f"severity:{expected.severity}",
                passed=report.severity == expected.severity,
                detail=f"got {report.severity}",
            )
        )
    if scenario.healthy:
        checks += [
            Check(name="no_incident:no_hypothesis", passed=not inv.hypotheses),
            Check(name="no_incident:confidence_0", passed=report.confidence == 0),
            Check(name="no_incident:severity_none", passed=report.severity == "none"),
        ]
        return checks
    text = (top.statement if top else "").casefold()
    checks.append(
        Check(
            name="root_cause_identified",
            passed=top is not None,
            detail=top.statement if top else "no root cause",
        )
    )
    for group in expected.root_cause_keywords:
        checks.append(
            Check(
                name=f"root_cause_mentions:{'|'.join(group)}",
                passed=any(word.casefold() in text for word in group),
            )
        )
    checks.append(
        Check(
            name=f"confidence>={expected.min_confidence}",
            passed=report.confidence >= expected.min_confidence,
            detail=f"{report.confidence:.2f}",
        )
    )
    cited = {e.id for e in inv.evidence}
    claims_ok = all(c.evidence_ids and set(c.evidence_ids) <= cited for c in inv.claims)
    checks.append(Check(name="claims_cite_evidence", passed=claims_ok))
    return checks


async def evaluate_investigations(
    settings: Settings, scenario_ids: Sequence[str] = ()
) -> list[InvestigationEval]:
    paths = EvalPaths.discover(settings.config_dir)
    scenarios = [
        s for s in load_scenarios(paths.scenarios) if not scenario_ids or s.id in scenario_ids
    ]
    evals: list[InvestigationEval] = []
    for scenario in scenarios:
        bus = EventBus()
        replay = ReplaySource(scenario=scenario, fixtures=paths.fixtures)
        orchestrator = Orchestrator(settings, replay=replay, bus=bus)
        inv = await orchestrator.investigate(InvestigationRequest(question="", mode="replay"))
        events = [e.type for e in bus.history(inv.id)]
        report = inv.report
        top = next(
            (h for h in inv.hypotheses if report and h.id == report.root_cause_hypothesis_id), None
        )
        evals.append(
            InvestigationEval(
                scenario=scenario.id,
                title=scenario.title,
                investigation_id=inv.id,
                status=inv.status.value,
                root_cause=top.statement if top else None,
                confidence=report.confidence if report else 0.0,
                severity=report.severity if report else "none",
                checks=score_investigation(scenario, inv, events),
                tokens=inv.usage.total_tokens,
                duration_ms=inv.duration_ms or 0.0,
            )
        )
    return evals


def run_investigation_evals(
    settings: Settings, scenario_ids: Sequence[str] = ()
) -> list[InvestigationEval]:
    return asyncio.run(evaluate_investigations(settings, scenario_ids))
