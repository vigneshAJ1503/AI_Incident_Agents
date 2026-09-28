"""Domain models shared by every agent, the orchestrator, the API and the UI.

These are the contracts: agents receive an ``AgentTask`` and return an ``AgentResult``.
Every claim is typed (FACT ... RECOMMENDATION) and points at evidence IDs, so a
hypothesis can never be presented as a verified fact.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DURATION = re.compile(r"^\s*(\d+)\s*(s|m|h|d|w)\s*$", re.IGNORECASE)
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_timestamp(value: Any) -> datetime | None:
    """ISO-8601 text ('2026-09-25T10:10:07.571Z') or a datetime -> aware UTC; else None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def parse_duration(text: str) -> timedelta:
    """'30m' -> 30 minutes. Supports s, m, h, d, w."""
    match = _DURATION.match(text)
    if not match:
        raise ValueError(f"Invalid duration '{text}'. Use e.g. 30m, 1h, 2d.")
    amount, unit = int(match.group(1)), match.group(2).lower()
    if amount <= 0:
        raise ValueError("Duration must be positive.")
    return timedelta(**{_UNITS[unit]: amount})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- enums


class ClaimKind(StrEnum):
    FACT = "FACT"  # directly observed and verifiable (e.g. "427 HTTP 500 logs")
    OBSERVATION = "OBSERVATION"  # derived from data (e.g. "error rate rose 10x")
    CORRELATION = "CORRELATION"  # two things co-occur in time/space
    HYPOTHESIS = "HYPOTHESIS"  # a proposed explanation, never presented as fact
    RECOMMENDATION = "RECOMMENDATION"  # a suggested action


class EvidenceKind(StrEnum):
    LOG = "log"
    METRIC = "metric"
    ALERT = "alert"
    K8S_EVENT = "k8s_event"
    COMMIT = "commit"
    TICKET = "ticket"
    DOC = "doc"


class AgentStatus(StrEnum):
    SUCCESS = "success"  # completed, found relevant signal
    NO_SIGNAL = "no_signal"  # completed, nothing abnormal
    PARTIAL = "partial"  # hit a limit or a data source failed; results incomplete
    FAILED = "failed"


class InvestigationStatus(StrEnum):
    PENDING = "pending"
    NEEDS_CLARIFICATION = "needs_clarification"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"  # POST /investigations/{id}/cancel (contract addendum)


class StepStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


# --------------------------------------------------------------------------- time


class TimeRange(_Model):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self

    @classmethod
    def last(cls, duration: str | timedelta, now: datetime | None = None) -> TimeRange:
        delta = parse_duration(duration) if isinstance(duration, str) else duration
        end = now or utcnow()
        return cls(start=end - delta, end=end)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def shifted(self, delta: timedelta) -> TimeRange:
        """Same-length window moved by ``delta`` (e.g. -24h for a baseline)."""
        return TimeRange(start=self.start + delta, end=self.end + delta)


# --------------------------------------------------------------------------- incident


class IncidentContext(_Model):
    """What every agent knows about the incident."""

    question: str
    service: str | None = None
    environment: str | None = None
    time_range: TimeRange
    symptoms: list[str] = Field(default_factory=list)  # enriched between rounds


class Incident(_Model):
    id: str = Field(default_factory=lambda: new_id("INC"))
    title: str
    description: str = ""
    service: str | None = None
    environment: str | None = None
    source: Literal["user", "alert", "ticket"] = "user"
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- evidence & claims


class Evidence(_Model):
    id: str = Field(default_factory=lambda: new_id("ev"))
    kind: EvidenceKind
    source: str  # capability/tool that produced it, e.g. "logs.execute_esql"
    summary: str
    link: str | None = None  # Kibana / Grafana / Jira deep link
    timestamp: datetime | None = None
    query: str | None = None  # the exact query, for reproducibility
    data: dict[str, Any] = Field(default_factory=dict)  # excerpt, series, diff hunk


class Finding(_Model):
    id: str = Field(default_factory=lambda: new_id("fd"))
    kind: ClaimKind
    type: str  # e.g. "error_pattern", "latency_spike"
    description: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _facts_need_evidence(self) -> Self:
        if self.kind in (ClaimKind.FACT, ClaimKind.OBSERVATION) and not self.evidence_ids:
            raise ValueError(f"{self.kind} findings must cite at least one evidence id")
        return self


class Hypothesis(_Model):
    id: str = Field(default_factory=lambda: new_id("hy"))
    statement: str
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: list[str] = Field(min_length=1)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)

    @property
    def kind(self) -> ClaimKind:
        return ClaimKind.HYPOTHESIS


class Recommendation(_Model):
    id: str = Field(default_factory=lambda: new_id("rc"))
    action: str
    rationale: str = ""
    risk: Literal["low", "medium", "high"] = "low"
    requires_approval: bool = False
    evidence_ids: list[str] = Field(default_factory=list)


class TimelineEvent(_Model):
    timestamp: datetime
    description: str
    source: str
    evidence_id: str | None = None


# --------------------------------------------------------------------------- execution


class TokenUsage(_Model):
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            calls=self.calls + other.calls,
        )


class ToolCall(_Model):
    id: str = Field(default_factory=lambda: new_id("tc"))
    agent: str
    capability: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["ok", "error", "blocked", "timeout"] = "ok"
    started_at: datetime = Field(default_factory=utcnow)
    duration_ms: float = 0.0
    result_chars: int = 0
    error: str | None = None


class AgentTask(_Model):
    id: str = Field(default_factory=lambda: new_id("task"))
    investigation_id: str | None = None
    agent: str
    objective: str
    context: IncidentContext
    hints: dict[str, Any] = Field(default_factory=dict)  # e.g. {"trace_ids": [...]} in round 2
    round: int = Field(default=1, ge=1)


class AgentResult(_Model):
    agent: str
    agent_version: str = "0"
    task_id: str
    status: AgentStatus
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)  # machine-usable, e.g. "db_timeout_errors_up"
    confidence: float | None = Field(default=None, ge=0, le=1)
    suggested_followups: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    prompt_version: str | None = None
    model: str | None = None
    error: str | None = None
    duration_ms: float = 0.0

    @model_validator(mode="after")
    def _evidence_references_exist(self) -> Self:
        known = {e.id for e in self.evidence}
        dangling = {eid for f in self.findings for eid in f.evidence_ids} - known
        if dangling:
            raise ValueError(f"findings cite unknown evidence ids: {sorted(dangling)}")
        return self


class InvestigationStep(_Model):
    id: str = Field(default_factory=lambda: new_id("step"))
    agent: str
    objective: str
    depends_on: list[str] = Field(default_factory=list)
    round: int = Field(default=1, ge=1)
    status: StepStatus = StepStatus.QUEUED
    started_at: datetime | None = None
    finished_at: datetime | None = None


Severity = Literal["critical", "high", "medium", "low", "none"]
InvestigationMode = Literal["live", "replay", "demo"]


class InvestigationReport(_Model):
    """The response builder's output (docs/api/contract.md): what the UI shows first."""

    summary: str
    root_cause_hypothesis_id: str | None = None  # None = no root cause identified
    confidence: float = Field(default=0.0, ge=0, le=1)
    impact: str = ""
    affected_services: list[str] = Field(default_factory=list)
    severity: Severity = "none"
    next_steps: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    markdown: str = ""


class Investigation(_Model):
    id: str = Field(default_factory=lambda: new_id("inv"))
    incident: Incident
    context: IncidentContext | None = None
    status: InvestigationStatus = InvestigationStatus.PENDING
    steps: list[InvestigationStep] = Field(default_factory=list)
    results: list[AgentResult] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    #: The RCA's typed claims (FACT ... RECOMMENDATION), each citing evidence ids.
    claims: list[Finding] = Field(default_factory=list)
    report: InvestigationReport | None = None  # None until the RCA phase finished
    clarification_question: str | None = None
    clarification_candidates: list[str] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)  # model / prompt / agent versions
    #: Aggregated tokens of the whole investigation (agents + planner + RCA).
    usage: TokenUsage = Field(default_factory=TokenUsage)
    duration_ms: float | None = None
    created_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    mode: InvestigationMode = "live"  # where the data came from

    @property
    def evidence(self) -> list[Evidence]:
        return [e for r in self.results for e in r.evidence]

    def results_usage(self) -> TokenUsage:
        """Tokens spent by the agents (``usage`` also counts the planner and the RCA)."""
        total = TokenUsage()
        for result in self.results:
            total = total + result.usage
        return total


#: Models exported as JSON Schema to docs/schemas (kept in sync by a unit test).
SCHEMA_MODELS: tuple[type[BaseModel], ...] = (
    Incident,
    IncidentContext,
    Investigation,
    InvestigationReport,
    InvestigationStep,
    AgentTask,
    AgentResult,
    Evidence,
    Finding,
    Hypothesis,
    Recommendation,
    ToolCall,
    TimelineEvent,
)
