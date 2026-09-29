"""The report timeline ("Why it happened"): the incident's own chain first, in time order;
related tickets/runbooks never dominate it (live-demo regression: OPS-17..OPS-24 from
earlier test runs sorted to the top and pushed the real chain down). Zero tokens."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiops.core.models import (
    AgentResult,
    AgentStatus,
    Evidence,
    EvidenceKind,
    Incident,
    IncidentContext,
    Investigation,
    TimeRange,
)
from aiops.orchestrator.response import MAX_TIMELINE, MAX_TIMELINE_CONTEXT, build_timeline

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)


def ev(kind: EvidenceKind, minutes_ago: float, summary: str) -> Evidence:
    return Evidence(
        kind=kind, source="x", summary=summary, timestamp=NOW - timedelta(minutes=minutes_ago)
    )


def res(
    agent: str, evidence: list[Evidence], status: AgentStatus = AgentStatus.SUCCESS
) -> AgentResult:
    return AgentResult(
        agent=agent, task_id=f"t-{agent}", status=status, summary="", evidence=evidence
    )


def investigation(results: list[AgentResult]) -> Investigation:
    return Investigation(
        incident=Incident(title="payments 500"),
        context=IncidentContext(
            question="payments 500",
            service="payment-service",
            time_range=TimeRange.last("30m", now=NOW),
        ),
        results=results,
    )


def test_old_tickets_follow_the_incident_chain_and_are_capped() -> None:
    # tickets ranked by the tickets agent (most relevant first), all older than the incident
    tickets = [ev(EvidenceKind.TICKET, 600 - i, f"OPS-{17 + i} old test ticket") for i in range(8)]
    inv = investigation(
        [
            res("tickets", tickets),
            res("alerts", [ev(EvidenceKind.ALERT, 5, "HighErrorRate fires")]),
            res("logs", [ev(EvidenceKind.LOG, 8, "5xx: Database connection timeout")]),
            res("metrics", [ev(EvidenceKind.METRIC, 10, "DB pool saturated")]),
            res("k8s", [ev(EvidenceKind.K8S_EVENT, 12, "Rollout of revision 14")]),
            res("code", [ev(EvidenceKind.COMMIT, 12, "DB_POOL_SIZE 20 -> 2")]),
        ]
    )
    timeline = build_timeline(inv)
    assert [t.description for t in timeline] == [
        "DB_POOL_SIZE 20 -> 2",  # same minute as the rollout: the change comes first
        "Rollout of revision 14",
        "DB pool saturated",
        "5xx: Database connection timeout",
        "HighErrorRate fires",
        "OPS-17 old test ticket",  # the two most relevant tickets, after the chain
        "OPS-18 old test ticket",
    ]
    assert MAX_TIMELINE_CONTEXT == 2


def test_context_never_crowds_out_the_chain_and_failed_agents_are_skipped() -> None:
    chain = [ev(EvidenceKind.LOG, 29 - i * 0.5, f"log {i}") for i in range(MAX_TIMELINE + 5)]
    inv = investigation(
        [
            res("knowledge", [ev(EvidenceKind.DOC, 60, "runbook")]),
            res("logs", chain),
            res("metrics", [ev(EvidenceKind.METRIC, 1, "failed")], status=AgentStatus.FAILED),
        ]
    )
    timeline = build_timeline(inv)
    assert len(timeline) == MAX_TIMELINE
    assert timeline[-1].description == "runbook"
    assert all(t.description != "failed" for t in timeline)
    stamps = [t.timestamp for t in timeline[:-1]]
    assert stamps == sorted(stamps)
