from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from aiops.core.models import (
    AgentResult,
    AgentStatus,
    AgentTask,
    ClaimKind,
    Evidence,
    EvidenceKind,
    Finding,
    Hypothesis,
    Incident,
    IncidentContext,
    Investigation,
    TimeRange,
    TokenUsage,
    parse_duration,
)

NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("30m", timedelta(minutes=30)), ("1h", timedelta(hours=1)), (" 2D ", timedelta(days=2))],
)
def test_parse_duration(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "0m", "10y", "-5m"])
def test_parse_duration_invalid(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


def test_time_range_last_and_shift() -> None:
    window = TimeRange.last("30m", now=NOW)
    assert window.start == NOW - timedelta(minutes=30)
    assert window.duration == timedelta(minutes=30)
    baseline = window.shifted(timedelta(hours=-24))
    assert baseline.end == NOW - timedelta(hours=24)


def test_time_range_rejects_naive_and_unordered() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        TimeRange(start=datetime(2026, 1, 1), end=datetime(2026, 1, 2))
    with pytest.raises(ValidationError, match="end must be after start"):
        TimeRange(start=NOW, end=NOW)


def _evidence() -> Evidence:
    return Evidence(
        kind=EvidenceKind.LOG,
        source="logs.execute_esql",
        summary="427 ERROR logs 'Database connection timeout'",
        query='FROM payment-prod-* | WHERE level == "ERROR"',
    )


def test_fact_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="must cite"):
        Finding(kind=ClaimKind.FACT, type="error_pattern", description="x")
    # Hypotheses/recommendations may be un-cited at finding level
    Finding(kind=ClaimKind.RECOMMENDATION, type="next_step", description="check pool")


def test_agent_result_rejects_dangling_evidence_ids() -> None:
    ev = _evidence()
    ok = Finding(
        kind=ClaimKind.FACT, type="error_pattern", description="db timeouts", evidence_ids=[ev.id]
    )
    AgentResult(
        agent="logs",
        task_id="t1",
        status=AgentStatus.SUCCESS,
        summary="s",
        findings=[ok],
        evidence=[ev],
    )
    bad = Finding(
        kind=ClaimKind.FACT, type="error_pattern", description="x", evidence_ids=["ev-missing"]
    )
    with pytest.raises(ValidationError, match="ev-missing"):
        AgentResult(
            agent="logs", task_id="t1", status=AgentStatus.SUCCESS, summary="s", findings=[bad]
        )


def test_hypothesis_needs_support_and_is_typed() -> None:
    with pytest.raises(ValidationError):
        Hypothesis(statement="pool misconfig", confidence=0.9, supporting_evidence_ids=[])
    hyp = Hypothesis(statement="pool misconfig", confidence=0.9, supporting_evidence_ids=["ev-1"])
    assert hyp.kind is ClaimKind.HYPOTHESIS


def test_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        Hypothesis(statement="x", confidence=1.5, supporting_evidence_ids=["ev-1"])


def test_investigation_aggregates_usage_and_evidence() -> None:
    ev = _evidence()
    ctx = IncidentContext(question="Payment API 500", time_range=TimeRange.last("1h", now=NOW))
    task = AgentTask(agent="logs", objective="find errors", context=ctx)
    result = AgentResult(
        agent="logs",
        task_id=task.id,
        status=AgentStatus.SUCCESS,
        summary="s",
        evidence=[ev],
        usage=TokenUsage(input_tokens=100, output_tokens=20, calls=1),
    )
    inv = Investigation(
        incident=Incident(title="Payment API 500"), context=ctx, results=[result, result]
    )
    assert inv.usage.total_tokens == 240
    assert inv.usage.calls == 2
    assert len(inv.evidence) == 2


def test_round_trip_json() -> None:
    ctx = IncidentContext(
        question="q", service="payment-service", time_range=TimeRange.last("30m", now=NOW)
    )
    task = AgentTask(agent="logs", objective="o", context=ctx, hints={"trace_ids": ["t-1"]})
    assert AgentTask.model_validate_json(task.model_dump_json()) == task


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        Incident.model_validate({"title": "x", "sevrity": "high"})
