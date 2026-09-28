"""RCA agent (PR-033) + response builder + demo data (PR-034). Zero tokens."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aiops.agents.rca_agent import RCAAgent
from aiops.agents.rca_agent.correlation import SOURCE_CONFIDENCE, correlate
from aiops.core.config import Settings, load_settings
from aiops.core.events import INVESTIGATION_EVENT_TYPES, EventBus
from aiops.core.models import (
    AgentResult,
    AgentStatus,
    ClaimKind,
    Evidence,
    EvidenceKind,
    IncidentContext,
    Investigation,
    InvestigationStatus,
    TimeRange,
)
from aiops.evals.investigation import run_investigation_evals
from aiops.llm.fake import FakeLLMProvider, tool_call
from aiops.orchestrator.demo import build_demo, export_demo, seed_demo, synthetic_history
from aiops.orchestrator.engine import InvestigationRequest, Orchestrator
from aiops.orchestrator.replay import load_replay
from aiops.store.repository import InvestigationStore

CONFIG = Path(__file__).resolve().parents[3] / "config"
NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
DEMO_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings("local", CONFIG)


def replay(settings: Settings, scenario: str) -> tuple[Investigation, EventBus]:
    bus = EventBus()
    orch = Orchestrator(settings, replay=load_replay(settings, scenario), bus=bus)
    return asyncio.run(orch.investigate(InvestigationRequest(question="", mode="replay"))), bus


# --------------------------------------------------------------------------- acceptance


def test_replay_evals_reach_ground_truth_s0_to_s5(settings: Settings) -> None:
    evals = run_investigation_evals(settings)
    assert [e.scenario for e in evals] == ["S0", "S1", "S2", "S3", "S4", "S5"]
    failed = {e.scenario: [c.name for c in e.checks if not c.passed] for e in evals if not e.passed}
    assert not failed, failed
    assert all(e.tokens == 0 for e in evals)


def test_s1_pool_misconfiguration_with_high_confidence(settings: Settings) -> None:
    inv, bus = replay(settings, "S1")
    report = inv.report
    assert report is not None and inv.status is InvestigationStatus.COMPLETED
    top = inv.hypotheses[0]
    assert report.root_cause_hypothesis_id == top.id
    assert "pool" in top.statement and "v1.8.2" in top.statement
    assert "DB_POOL_SIZE 20 -> 2" in top.statement
    assert report.confidence >= 0.8
    assert report.severity == "critical"
    assert report.affected_services == ["payment-service"]
    assert report.next_steps and any("Roll back" in s for s in report.next_steps)
    # every claim is typed and cites existing evidence
    evidence = {e.id for e in inv.evidence}
    kinds = {c.kind for c in inv.claims}
    assert {ClaimKind.OBSERVATION, ClaimKind.CORRELATION, ClaimKind.HYPOTHESIS} <= kinds
    assert ClaimKind.RECOMMENDATION in kinds
    assert all(c.evidence_ids and set(c.evidence_ids) <= evidence for c in inv.claims)
    assert set(top.supporting_evidence_ids) <= evidence
    # time alignment: the change preceded the first error
    assert any(c.type == "change_before_first_error" for c in inv.claims)
    # the timeline is chronological and comes from evidence
    stamps = [t.timestamp for t in inv.timeline]
    assert stamps == sorted(stamps) and all(t.evidence_id in evidence for t in inv.timeline)
    # markdown has the PR-034 sections
    for section in (
        "## Summary",
        "## Incident and impact",
        "## Root cause hypothesis",
        "## Timeline",
        "## Key findings",
        "## Logs",
        "## Metrics",
        "## Kubernetes state",
        "## Recent changes",
        "## Next steps",
    ):
        assert section in report.markdown
    types = [e.type for e in bus.history(inv.id)]
    assert types[-4:] == [
        "rca_started",
        "hypothesis_ranked",
        "report_ready",
        "investigation_finished",
    ]
    assert set(types) <= set(INVESTIGATION_EVENT_TYPES)


def test_s0_no_incident(settings: Settings) -> None:
    inv, _ = replay(settings, "S0")
    report = inv.report
    assert report is not None
    assert inv.hypotheses == [] and report.root_cause_hypothesis_id is None
    assert report.confidence == 0 and report.severity == "none"
    assert report.summary.startswith("No incident detected")
    assert "No root cause identified" in report.markdown


# --------------------------------------------------------------------------- rules


def result(
    agent: str, signals: list[str], kind: EvidenceKind, ts: datetime | None = None
) -> AgentResult:
    evidence = Evidence(kind=kind, source=f"{agent}.x", summary=f"{agent} evidence", timestamp=ts)
    return AgentResult(
        agent=agent,
        task_id=f"t-{agent}",
        status=AgentStatus.SUCCESS,
        summary=f"{agent} summary",
        evidence=[evidence],
        signals=signals,
    )


def context() -> IncidentContext:
    return IncidentContext(
        question="payments 500",
        service="payment-service",
        time_range=TimeRange.last("30m", now=NOW),
    )


def test_one_source_never_exceeds_0_7_and_weak_evidence_is_no_root_cause() -> None:
    weak = [result("logs", ["db_timeout_errors_up"], EvidenceKind.LOG)]
    correlation = correlate(weak, service="payment-service")
    assert correlation.candidates[0].confidence <= 0.55
    outcome = asyncio.run(RCAAgent().analyze(context(), weak))
    assert outcome.hypotheses and outcome.root_cause is None  # weak: no root cause
    assert SOURCE_CONFIDENCE[1] < 0.7


def test_contradicting_evidence_is_listed_and_lowers_confidence() -> None:
    base = [
        result("logs", ["db_timeout_errors_up"], EvidenceKind.LOG),
        result("code", ["risky_config_change"], EvidenceKind.COMMIT),
    ]
    clean = correlate(base, service="payment-service").candidates[0]
    contradicted = correlate(
        [*base, result("metrics", ["no_anomaly"], EvidenceKind.METRIC)], service="payment-service"
    ).candidates[0]
    assert contradicted.confidence < clean.confidence
    assert contradicted.contradicting_evidence and contradicted.contradicting == ["metrics"]


def test_time_alignment_bonus() -> None:
    change = result(
        "code", ["risky_config_change"], EvidenceKind.COMMIT, NOW - timedelta(minutes=25)
    )
    error = result("logs", ["db_timeout_errors_up"], EvidenceKind.LOG, NOW - timedelta(minutes=20))
    aligned = correlate([change, error], service="payment-service")
    assert aligned.alignment.aligned
    late = result("code", ["risky_config_change"], EvidenceKind.COMMIT, NOW - timedelta(minutes=5))
    assert not correlate([late, error], service="payment-service").alignment.aligned
    assert (
        aligned.candidates[0].confidence
        > correlate([late, error], service="payment-service").candidates[0].confidence
    )


def test_llm_only_ranks_and_phrases_never_invents(settings: Settings) -> None:
    results = [
        result("logs", ["db_timeout_errors_up", "error_rate_up"], EvidenceKind.LOG),
        result("metrics", ["db_pool_saturated", "latency_up"], EvidenceKind.METRIC),
        result("code", ["risky_config_change"], EvidenceKind.COMMIT),
    ]
    llm = FakeLLMProvider(
        [
            tool_call(
                "submit",
                {
                    "ranked": [
                        {"id": "invented_cause", "statement": "Aliens"},
                        {"id": "db_pool_misconfiguration", "statement": "Pool too small."},
                    ]
                },
            )
        ]
    )
    outcome = asyncio.run(RCAAgent(llm).analyze(context(), results))
    assert llm.requests[0]["role"] == "rca"
    assert outcome.hypotheses[0].statement == "Pool too small."
    assert all("Aliens" not in h.statement for h in outcome.hypotheses)
    # an LLM error keeps the deterministic order
    outcome = asyncio.run(RCAAgent(FakeLLMProvider()).analyze(context(), results))
    assert outcome.hypotheses and any("unavailable" in n for n in outcome.notes)


# --------------------------------------------------------------------------- demo


def test_demo_history_is_deterministic_and_seeds_the_store(
    tmp_path: Path, settings: Settings
) -> None:
    data = asyncio.run(build_demo(settings, DEMO_NOW))
    assert len(data.investigations) == 46 and all(i.mode == "demo" for i in data.investigations)
    assert all(i.usage.total_tokens == 0 for i in data.investigations)
    assert all(i.created_at <= DEMO_NOW for i in data.investigations)
    assert min(i.created_at for i in data.investigations) >= DEMO_NOW - timedelta(days=14)
    statuses = {i.status for i in data.investigations}
    assert InvestigationStatus.COMPLETED in statuses and InvestigationStatus.FAILED in statuses
    replays = [
        i for i in data.investigations if i.id in data.events and len(data.events[i.id]) > 50
    ]
    assert replays
    # same seed + now -> same history shape
    again = synthetic_history(
        type(data)(
            investigations=data.investigations[-6:], events=data.events, scenarios=data.scenarios
        ),
        DEMO_NOW,
    )
    first = synthetic_history(
        type(data)(
            investigations=data.investigations[-6:], events=data.events, scenarios=data.scenarios
        ),
        DEMO_NOW,
    )
    assert [(i.created_at, i.status) for i in again.investigations] == [
        (i.created_at, i.status) for i in first.investigations
    ]

    store = InvestigationStore(f"sqlite:///{tmp_path / 'demo.db'}")
    store.migrate()

    async def seed() -> int:
        count = await seed_demo(store, data)
        count2 = await seed_demo(store, data)  # idempotent: replaces demo data
        assert count == count2 == await store.count()
        await store.close()
        return count

    assert asyncio.run(seed()) == 46

    written = export_demo(settings, data, tmp_path / "export", DEMO_NOW)
    names = {p.relative_to(tmp_path / "export").as_posix() for p in written}
    for name in (
        "investigations.json",
        "dashboard.json",
        "services.json",
        "agents.json",
        "approvals.json",
        "scenarios.json",
        "manifest.json",
    ):
        assert name in names
    dashboard = json.loads((tmp_path / "export" / "dashboard.json").read_text())
    assert dashboard["totals"]["investigations"] == 46
    assert len(dashboard["by_day"]) == 14 and len(dashboard["recent"]) == 5
    assert dashboard["mttr_minutes"]["p50"] > 0
    listing = json.loads((tmp_path / "export" / "investigations.json").read_text())
    assert len(listing["items"]) == 46 and listing["next_cursor"] is None
    one = listing["items"][0]["id"]
    Investigation.model_validate_json(
        (tmp_path / "export" / "investigations" / f"{one}.json").read_text()
    )
    events = json.loads((tmp_path / "export" / "events" / f"{one}.json").read_text())
    assert {e["type"] for e in events} <= set(INVESTIGATION_EVENT_TYPES)


def test_demo_history_ends_now_and_every_timestamp_is_consistent(settings: Settings) -> None:
    """Regression (PR-039): replays run 'now' on fixtures recorded earlier, so re-dating
    the run with the data's delta pushed steps/tool calls/incident.created_at into the
    future. Every timestamp must be <= now and inside the run it belongs to."""
    data = asyncio.run(build_demo(settings, DEMO_NOW))
    slack = timedelta(seconds=1)
    for inv in data.investigations:
        assert inv.completed_at is not None
        assert inv.created_at <= inv.completed_at <= DEMO_NOW, inv.id
        run = [inv.incident.created_at]
        run += [t for s in inv.steps for t in (s.started_at, s.finished_at) if t is not None]
        run += [c.started_at for r in inv.results for c in r.tool_calls]
        for t in run:
            assert inv.created_at - slack <= t <= inv.completed_at + slack, (inv.id, t)
        data_times = [t.timestamp for t in inv.timeline]
        data_times += [e.timestamp for r in inv.results for e in r.evidence if e.timestamp]
        if inv.context is not None:
            data_times.append(inv.context.time_range.end)
        assert all(t <= inv.completed_at for t in data_times), inv.id
        events = data.events.get(inv.id, [])
        assert all(e.timestamp <= DEMO_NOW for e in events), inv.id
        assert [e.timestamp for e in events] == sorted(e.timestamp for e in events)
    newest = max(i.completed_at or i.created_at for i in data.investigations)
    assert DEMO_NOW - timedelta(hours=2) <= newest <= DEMO_NOW
