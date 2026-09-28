"""Request/response models of the REST API, mirroring docs/api/contract.md.

The domain models (``Investigation``, ``InvestigationEvent``, ...) are reused as they are;
these add the API-only shapes. Fields marked "additive" are extensions of the contract
(the UI's schemas ignore unknown fields). Request bodies ignore unknown fields too.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aiops.core.models import Incident, InvestigationMode, InvestigationStatus

CapabilityState = Literal["ok", "down", "disabled"]
Severity = Literal["critical", "high", "medium", "low", "none"]
#: Approval states the UI knows. The approval framework also has ``rejected`` (policy)
#: and ``expired`` (TTL); they are reported as ``denied`` with the exact value in
#: ``lifecycle_status`` (contract addendum, PR-035).
ApprovalState = Literal["pending", "approved", "denied", "executed", "failed"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# --------------------------------------------------------------------------- errors


class ErrorDetail(_Out):
    code: str
    message: str


class ErrorResponse(_Out):
    error: ErrorDetail


# --------------------------------------------------------------------------- health, catalog


class LLMHealth(_Out):
    provider: str | None
    configured: bool


class HealthResponse(_Out):
    status: Literal["ok", "degraded"]
    version: str
    profile: str
    llm: LLMHealth
    capabilities: dict[str, CapabilityState]
    faults_enabled: bool
    store: Literal["ok", "down"]  # additive (PR-035)


class ServiceOut(_Out):
    name: str
    description: str = ""
    owners: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)
    runbooks: list[str] = Field(default_factory=list)


class AgentOut(_Out):
    name: str
    version: str
    description: str
    capabilities: list[str]
    last_run_at: datetime | None = None
    success_rate_7d: float | None = None
    p50_ms: float | None = None
    runs_7d: int = 0  # additive (PR-035)
    enabled: bool = True  # additive: its capabilities are enabled in this profile


# --------------------------------------------------------------------------- dashboard


class DashboardTotals(_Out):
    investigations: int
    open: int
    root_cause_found: int
    no_incident: int
    failed: int


class Mttr(_Out):
    p50: float
    p90: float


class DashboardDay(_Out):
    date: str
    investigations: int
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0


class DashboardService(_Out):
    service: str
    investigations: int
    top_root_cause: str | None = None


class DashboardSignal(_Out):
    signal: str
    count: int


class DashboardAgent(_Out):
    name: str
    runs: int
    success_rate: float
    p50_ms: float
    tokens: int = 0


class SummaryReport(_Out):
    summary: str
    severity: Severity
    confidence: float


class InvestigationSummary(_Out):
    id: str
    incident: Incident
    status: InvestigationStatus
    report: SummaryReport | None = None
    affected_services: list[str] = Field(default_factory=list)
    created_at: datetime
    completed_at: datetime | None = None
    duration_ms: float | None = None
    mode: InvestigationMode


class DashboardSummary(_Out):
    window_days: int
    totals: DashboardTotals
    mttr_minutes: Mttr
    avg_confidence: float
    by_day: list[DashboardDay]
    by_service: list[DashboardService]
    top_signals: list[DashboardSignal]
    agents: list[DashboardAgent]
    recent: list[InvestigationSummary]


# --------------------------------------------------------------------------- investigations


class InvestigationPage(_Out):
    items: list[InvestigationSummary]
    next_cursor: str | None = None


class CreateInvestigation(_In):
    question: str = Field(min_length=1, max_length=2000)
    service: str | None = None
    environment: str | None = None
    since: str | None = Field(default=None, max_length=20)
    start: datetime | None = None
    end: datetime | None = None
    mode: Literal["live", "replay"] | None = None
    #: Additive (PR-035): replay this scenario's recorded fixtures (S0-S5).
    scenario: str | None = None


class CreatedInvestigation(_Out):
    id: str
    status: InvestigationStatus
    mode: Literal["live", "replay"]  # additive: what was chosen
    scenario: str | None = None  # additive: the replayed scenario


class ClarifyRequest(_In):
    answer: str = Field(min_length=1, max_length=500)


class InvestigationState(_Out):
    id: str
    status: InvestigationStatus


class TicketDraftRequest(_In):
    #: Add the findings as a comment on this existing ticket instead of creating one.
    comment_on: str | None = None
    issue_type: str = "Bug"
    requested_by: str = "aiops-web"


class TicketDraftResponse(_Out):
    approval_id: str
    status: ApprovalState  # additive
    summary: str  # additive: the drafted ticket title


# --------------------------------------------------------------------------- approvals


class ApprovalOut(_Out):
    id: str
    action: str
    capability: str
    tool: str
    arguments: dict[str, Any]
    reason: str
    risk: Literal["low", "medium", "high"]
    status: ApprovalState
    lifecycle_status: str  # additive: the exact framework status (incl. rejected/expired)
    requested_by: str
    investigation_id: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    policy_reason: str | None = None


class DecisionRequest(_In):
    by: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=2000)


# --------------------------------------------------------------------------- scenarios


class ScenarioOut(_Out):
    id: str
    title: str
    service: str
    description: str = ""
    active: bool = False
    injectable: bool = True  # additive: S0 (healthy) has no fault


class FaultResult(_Out):
    scenario: str | None
    status: Literal["injected", "reverting", "reverted"]
    message: str


# --------------------------------------------------------------------------- ask (PR-041)


class AskRequest(_In):
    question: str = Field(min_length=1, max_length=2000)
    service: str | None = None
    environment: str | None = None
    since: str | None = Field(default=None, max_length=20)


class AskLink(_Out):
    label: str
    href: str  # a Web UI path, e.g. /investigations/inv-..., /agents


class RunningAgent(_Out):
    agent: str
    round: int
    status: str  # queued | running | done | failed | skipped
    tool: str | None = None  # the last tool it called (from the event log)


class RunningInvestigationItem(_Out):
    type: Literal["running_investigation"] = "running_investigation"
    id: str
    question: str
    service: str | None = None
    status: InvestigationStatus
    mode: InvestigationMode
    created_at: datetime
    elapsed_s: float
    round: int | None = None
    agents: list[RunningAgent] = Field(default_factory=list)
    href: str


class InvestigationItem(_Out):
    type: Literal["investigation"] = "investigation"
    id: str
    title: str
    service: str | None = None
    status: InvestigationStatus
    severity: Severity | None = None
    root_cause: str | None = None
    confidence: float | None = None
    created_at: datetime
    href: str


class AgentItem(_Out):
    type: Literal["agent"] = "agent"
    name: str
    description: str
    capabilities: list[str]
    providers: list[str]  # the profile's provider per capability (e.g. prometheus)
    enabled: bool
    success_rate_7d: float | None = None
    p50_ms: float | None = None
    runs_7d: int = 0


class CheckItem(_Out):
    type: Literal["check"] = "check"
    name: str
    status: Literal["ok", "down", "disabled", "info"]
    detail: str = ""


class SuggestionItem(_Out):
    type: Literal["suggestion"] = "suggestion"
    question: str
    hint: str = ""
    scenario: str | None = None  # the recorded scenario this question replays


AskItem = Annotated[
    RunningInvestigationItem | InvestigationItem | AgentItem | CheckItem | SuggestionItem,
    Field(discriminator="type"),
]


class AskAnswer(_Out):
    title: str
    markdown: str
    items: list[AskItem] = Field(default_factory=list)
    links: list[AskLink] = Field(default_factory=list)


class AskResponse(_Out):
    """``kind: platform`` -> ``answer``; ``kind: incident`` -> ``investigation_id`` (started),
    or ``answer`` with scenario suggestions when replay mode has no matching scenario."""

    kind: Literal["platform", "incident"]
    intent: str
    confidence: float
    source: Literal["rules", "llm", "default"]
    answer: AskAnswer | None = None
    investigation_id: str | None = None
    mode: Literal["live", "replay"] | None = None
    scenario: str | None = None
