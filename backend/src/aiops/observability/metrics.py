"""Prometheus metrics of the agents, LLM calls, tool calls, cost and human overrides.

Served by the API on ``/metrics`` (``observability.metrics.enabled``). Label cardinality is
bounded by construction: labels are agent names, model roles, configured model ids,
capability + allowlisted tool names, modes and statuses. Investigation ids, questions and
services are NEVER labels (they belong in traces).

The human override rate is a PromQL ratio over ``aiops_approval_decisions_total``
(``decision="denied"`` / all decisions); see the Grafana dashboard "AI Observability".
"""

from __future__ import annotations

from collections.abc import Iterable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from aiops.core.config import Settings
from aiops.core.models import AgentResult, AgentStatus, Investigation, InvestigationStatus
from aiops.llm.factory import llm_configured

NS = "aiops"
#: Seconds. Agents run 0.1 s (replay) to ~2 min (live, max_execution_s).
AGENT_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 90, 120, 180, 300)
LLM_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120)
TOOL_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
INVESTIGATION_BUCKETS = (1, 5, 10, 30, 60, 120, 180, 300, 600, 900, 1800)
CONFIDENCE_BUCKETS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
COST_BUCKETS = (0.0, 0.0001, 0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 5)
#: Placeholder label value for a tool name outside the capability's allowlist (e.g. a
#: model hallucinating a tool), so arbitrary strings can't create new series.
UNLISTED_TOOL = "_unlisted"

INVESTIGATIONS = Counter(
    "investigations", "Investigations finished, by status.", ["mode", "status"], namespace=NS
)
INVESTIGATION_DURATION = Histogram(
    "investigation_duration_seconds",
    "Wall-clock time of one investigation.",
    ["mode"],
    namespace=NS,
    buckets=INVESTIGATION_BUCKETS,
)
INVESTIGATION_COST = Histogram(
    "investigation_cost_usd",
    "Estimated LLM cost of one investigation (cost.pricing).",
    ["mode"],
    namespace=NS,
    buckets=COST_BUCKETS,
)
INVESTIGATION_CONFIDENCE = Histogram(
    "investigation_confidence",
    "Report confidence of investigations that named a root cause.",
    ["mode"],
    namespace=NS,
    buckets=CONFIDENCE_BUCKETS,
)
AGENT_RUNS = Counter(
    "agent_runs", "Agent runs, by agent and result status.", ["agent", "status"], namespace=NS
)
AGENT_DURATION = Histogram(
    "agent_duration_seconds",
    "Wall-clock time of one agent run.",
    ["agent"],
    namespace=NS,
    buckets=AGENT_BUCKETS,
)
AGENT_CONFIDENCE = Histogram(
    "agent_confidence",
    "Confidence an agent reported for its result.",
    ["agent"],
    namespace=NS,
    buckets=CONFIDENCE_BUCKETS,
)
AGENT_COST = Counter(
    "agent_cost_usd", "Estimated LLM cost spent by agent runs.", ["agent"], namespace=NS
)
AGENT_FALLBACKS = Counter(
    "agent_fallbacks",
    "Agent runs finished with the deterministic analysis instead of the LLM.",
    ["agent", "reason"],  # reason: llm_error | agent_budget | investigation_budget
    namespace=NS,
)
BUDGET_SKIPS = Counter(
    "budget_skipped_steps",
    "Investigation steps skipped because the investigation budget was spent.",
    ["agent"],
    namespace=NS,
)
LLM_REQUESTS = Counter(
    "llm_requests",
    "LLM calls, by caller, model role, model and outcome.",
    ["agent", "role", "model", "status"],  # status: ok | error | rate_limited
    namespace=NS,
)
LLM_DURATION = Histogram(
    "llm_request_duration_seconds",
    "Latency of one LLM call.",
    ["role", "model"],
    namespace=NS,
    buckets=LLM_BUCKETS,
)
LLM_TOKENS = Counter(
    "llm_tokens",
    "LLM tokens, by caller, model role, model and direction.",
    ["agent", "role", "model", "direction"],  # direction: input | output
    namespace=NS,
)
LLM_COST = Counter(
    "llm_cost_usd",
    "Estimated LLM cost (cost.pricing), by caller, model role and model.",
    ["agent", "role", "model"],
    namespace=NS,
)
TOOL_CALLS = Counter(
    "tool_calls",
    "Tool calls, by capability, tool, status and whether the cache answered.",
    ["capability", "tool", "status", "cached"],
    namespace=NS,
)
TOOL_DURATION = Histogram(
    "tool_call_duration_seconds",
    "Latency of one (uncached) tool call.",
    ["capability", "tool"],
    namespace=NS,
    buckets=TOOL_BUCKETS,
)
APPROVAL_DECISIONS = Counter(
    "approval_decisions",
    "Human decisions on write-action proposals (override rate = denied / all).",
    ["action", "decision"],  # decision: approved | denied
    namespace=NS,
)


def record_llm_call(
    *,
    agent: str,
    role: str,
    model: str,
    status: str,
    duration_s: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    LLM_REQUESTS.labels(agent, role, model, status).inc()
    LLM_DURATION.labels(role, model).observe(duration_s)
    if input_tokens:
        LLM_TOKENS.labels(agent, role, model, "input").inc(input_tokens)
    if output_tokens:
        LLM_TOKENS.labels(agent, role, model, "output").inc(output_tokens)
    if cost_usd:
        LLM_COST.labels(agent, role, model).inc(cost_usd)


def record_tool_call(
    *, capability: str, tool: str, status: str, cached: bool, duration_s: float
) -> None:
    TOOL_CALLS.labels(capability, tool, status, "true" if cached else "false").inc()
    if not cached:
        TOOL_DURATION.labels(capability, tool).observe(duration_s)


def record_agent(result: AgentResult) -> None:
    AGENT_RUNS.labels(result.agent, result.status.value).inc()
    AGENT_DURATION.labels(result.agent).observe(result.duration_ms / 1000)
    if result.confidence is not None:
        AGENT_CONFIDENCE.labels(result.agent).observe(result.confidence)
    if result.usage.cost_usd:
        AGENT_COST.labels(result.agent).inc(result.usage.cost_usd)


def record_investigation(inv: Investigation) -> None:
    INVESTIGATIONS.labels(inv.mode, inv.status.value).inc()
    if inv.duration_ms is not None:
        INVESTIGATION_DURATION.labels(inv.mode).observe(inv.duration_ms / 1000)
    INVESTIGATION_COST.labels(inv.mode).observe(inv.usage.cost_usd)
    report = inv.report
    if report is not None and report.root_cause_hypothesis_id is not None:
        INVESTIGATION_CONFIDENCE.labels(inv.mode).observe(report.confidence)


def record_fallback(agent: str, reason: str) -> None:
    AGENT_FALLBACKS.labels(agent, reason).inc()


def record_budget_skip(agent: str) -> None:
    BUDGET_SKIPS.labels(agent).inc()


def record_approval(action: str, decision: str) -> None:
    APPROVAL_DECISIONS.labels(action, decision).inc()


def initialize(settings: Settings, agents: Iterable[str], actions: Iterable[str] = ()) -> None:
    """Create the profile's bounded series at 0 (API startup), so the first event after a
    scrape is an increase Prometheus can see (a series born at 1 is invisible to
    ``increase()``/``rate()``). Every label value comes from the profile or the registry."""
    agents = list(agents)
    for mode in ("live", "replay", "demo"):
        for status in InvestigationStatus:
            INVESTIGATIONS.labels(mode, status.value)
        INVESTIGATION_DURATION.labels(mode)
        INVESTIGATION_COST.labels(mode)
        INVESTIGATION_CONFIDENCE.labels(mode)
    for agent in agents:
        for agent_status in AgentStatus:
            AGENT_RUNS.labels(agent, agent_status.value)
        AGENT_DURATION.labels(agent)
        AGENT_CONFIDENCE.labels(agent)
    for action in actions:
        for decision in ("approved", "denied"):
            APPROVAL_DECISIONS.labels(action, decision)
    models = settings.llm.models_by_role()
    callers = {a: settings.agent(a).model_role for a in agents}
    callers.update({"planner": "fast", "rca": "rca", "intent": "fast"})
    # Without a usable hosted LLM, calls go to the scripted stand-in (label model="fake").
    fake = not llm_configured(settings.llm)
    for caller, role in callers.items():
        model = "fake" if fake else models.get(role, "-")
        LLM_REQUESTS.labels(caller, role, model, "ok")
        LLM_DURATION.labels(role, model)
        for direction in ("input", "output"):
            LLM_TOKENS.labels(caller, role, model, direction)
        LLM_COST.labels(caller, role, model)
    for name, capability in settings.capabilities.items():
        if capability.enabled:
            for tool in capability.tool_allowlist:
                TOOL_CALLS.labels(name, tool, "ok", "false")
                TOOL_DURATION.labels(name, tool)


def render(registry: CollectorRegistry = REGISTRY) -> tuple[bytes, str]:
    """The Prometheus text exposition and its content type."""
    return generate_latest(registry), CONTENT_TYPE_LATEST
