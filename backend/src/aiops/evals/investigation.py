"""Whole-investigation evals (PR-034, extended in PR-040): run S0-S5 end to end and score
the RCA.

Ground truth per scenario (``expected.yaml``):

* ``root_cause`` (text) and ``investigation.root_cause_keywords``: groups of
  alternatives; the top hypothesis must mention one word of **every** group;
* ``investigation.min_confidence`` for the root cause, ``investigation.severity``;
* healthy scenarios (``root_cause: null``) must end with "no incident": no hypothesis,
  confidence 0, severity ``none``.

Modes: ``replay`` (recorded fixtures, fake LLM, zero tokens) and ``live`` (each scenario
seeded into the local stack now, then investigated with the configured hosted LLM).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence

from pydantic import BaseModel, Field

from aiops.core.config import Settings
from aiops.core.events import INVESTIGATION_EVENT_TYPES, EventBus
from aiops.core.models import Investigation, InvestigationStatus
from aiops.evals.judge import JudgeVerdict
from aiops.evals.runner import EvalPaths, LLMFactory, Mode, require_live_llm, seed_live
from aiops.evals.scenario import Scenario, load_scenarios
from aiops.evals.scoring import Check
from aiops.orchestrator.engine import InvestigationRequest, Orchestrator
from aiops.orchestrator.replay import ReplaySource

#: Checks that decide root-cause accuracy (the rest: confidence, severity, contract, ...).
ROOT_CAUSE_CHECKS = ("root_cause_identified", "root_cause_mentions:", "no_incident:no_hypothesis")


class InvestigationEval(BaseModel):
    scenario: str
    title: str
    investigation_id: str
    status: str
    healthy: bool = False
    expected_service: str | None = None
    service: str | None = None  # what the investigation ran on
    root_cause: str | None
    confidence: float
    severity: str
    checks: list[Check] = Field(default_factory=list)
    claims: int = 0
    valid_claims: int = 0  # cite at least one evidence id, all of which exist
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: float = 0.0
    models: list[str] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)
    judge: JudgeVerdict | None = None
    judge_error: str | None = None
    cost_usd: float = 0.0

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def root_cause_correct(self) -> bool:
        """Rule-based: the right root cause (or, on a healthy scenario, none at all)."""
        relevant = [c for c in self.checks if c.name.startswith(ROOT_CAUSE_CHECKS)]
        return bool(relevant) and all(c.passed for c in relevant)

    @property
    def false_positive(self) -> bool:
        """A healthy scenario reported as an incident."""
        return self.healthy and (self.root_cause is not None or self.severity != "none")

    @property
    def brier(self) -> float:
        """(confidence - outcome)^2, the outcome being "the reported root cause is right".
        A healthy scenario has no right root cause: any confidence is miscalibrated."""
        outcome = 1.0 if self.root_cause_correct and not self.healthy else 0.0
        return round((self.confidence - outcome) ** 2, 6)


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


def investigation_eval(
    scenario: Scenario, inv: Investigation, events: Sequence[str]
) -> InvestigationEval:
    report = inv.report
    top = next(
        (h for h in inv.hypotheses if report and h.id == report.root_cause_hypothesis_id), None
    )
    cited = {e.id for e in inv.evidence}
    valid_claims = sum(bool(c.evidence_ids) and set(c.evidence_ids) <= cited for c in inv.claims)
    return InvestigationEval(
        scenario=scenario.id,
        title=scenario.title,
        investigation_id=inv.id,
        status=inv.status.value,
        healthy=scenario.healthy,
        expected_service=scenario.service,
        service=inv.context.service if inv.context else None,
        root_cause=top.statement if top else None,
        confidence=report.confidence if report else 0.0,
        severity=report.severity if report else "none",
        checks=score_investigation(scenario, inv, events),
        claims=len(inv.claims),
        valid_claims=valid_claims,
        tokens=inv.usage.total_tokens,
        input_tokens=inv.usage.input_tokens,
        output_tokens=inv.usage.output_tokens,
        duration_ms=inv.duration_ms or 0.0,
        models=sorted({r.model for r in inv.results if r.model}),
        versions=dict(inv.versions),
    )


async def run_one(
    settings: Settings,
    scenario: Scenario,
    paths: EvalPaths,
    *,
    mode: Mode = "replay",
    llm_factory: LLMFactory | None = None,
    es_url: str | None = None,
) -> InvestigationEval:
    bus = EventBus()
    if mode == "replay":
        replay = ReplaySource(scenario=scenario, fixtures=paths.fixtures)
        orchestrator = Orchestrator(settings, replay=replay, bus=bus)
        request = InvestigationRequest(question="", mode="replay")
    else:
        from aiops.agents.registry import AGENTS

        capabilities = sorted({c for spec in AGENTS.specs() for c in spec.capabilities})
        es = es_url or os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")
        now = await asyncio.to_thread(seed_live, scenario, capabilities, es)
        llm = llm_factory() if llm_factory else None
        orchestrator = Orchestrator(settings, llm=llm, bus=bus)
        # Only the question: identifying the service is part of what is evaluated.
        request = InvestigationRequest(
            question=scenario.question, since=scenario.window, end=now, mode="live"
        )
    inv = await orchestrator.investigate(request)
    return investigation_eval(scenario, inv, [e.type for e in bus.history(inv.id)])


async def evaluate_investigations(
    settings: Settings,
    scenario_ids: Sequence[str] = (),
    *,
    mode: Mode = "replay",
    llm_factory: LLMFactory | None = None,
    paths: EvalPaths | None = None,
    on_result: Callable[[InvestigationEval], None] | None = None,
) -> list[InvestigationEval]:
    paths = paths or EvalPaths.discover(settings.config_dir)
    wanted = {s.upper() for s in scenario_ids}
    scenarios = [s for s in load_scenarios(paths.scenarios) if not wanted or s.id.upper() in wanted]
    if mode == "live" and llm_factory is None:
        require_live_llm(settings)
    evals: list[InvestigationEval] = []
    for scenario in scenarios:
        result = await run_one(settings, scenario, paths, mode=mode, llm_factory=llm_factory)
        evals.append(result)
        if on_result:
            on_result(result)
    return evals


def run_investigation_evals(
    settings: Settings, scenario_ids: Sequence[str] = ()
) -> list[InvestigationEval]:
    return asyncio.run(evaluate_investigations(settings, scenario_ids))
