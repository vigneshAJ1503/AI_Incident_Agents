"""Response builder (PR-034): RCA outcome + results -> timeline, report, markdown.

The ``report`` block is exactly the contract's (docs/api/contract.md): summary, root
cause hypothesis id, confidence, impact, affected services, severity, next steps, open
questions and the full markdown report (for copy/export/Jira).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from aiops.agents.base import SUSPECTED_INJECTION
from aiops.agents.rca_agent import RCAOutcome
from aiops.core.config import SeverityRules
from aiops.core.models import (
    AgentStatus,
    ClaimKind,
    EvidenceKind,
    Investigation,
    InvestigationReport,
    Severity,
    TimelineEvent,
)
from aiops.core.signals import BENIGN_SIGNALS

TIMELINE_LOOKBACK = timedelta(hours=24)
MAX_TIMELINE = 25
#: Related tickets/runbooks shown after the incident's own chain of events.
MAX_TIMELINE_CONTEXT = 2
TIMELINE_CONTEXT_KINDS = (EvidenceKind.TICKET, EvidenceKind.DOC)
#: Tie-break at equal timestamps: cause before effect.
CHAIN_ORDER = {
    EvidenceKind.COMMIT: 0,
    EvidenceKind.K8S_EVENT: 1,
    EvidenceKind.METRIC: 2,
    EvidenceKind.LOG: 3,
    EvidenceKind.ALERT: 4,
}
IMPACT_KINDS = (EvidenceKind.METRIC, EvidenceKind.LOG)
SECTIONS: dict[str, tuple[EvidenceKind, ...]] = {
    "Logs": (EvidenceKind.LOG,),
    "Metrics": (EvidenceKind.METRIC,),
    "Alerts": (EvidenceKind.ALERT,),
    "Kubernetes state": (EvidenceKind.K8S_EVENT,),
    "Recent changes": (EvidenceKind.COMMIT,),
    "Runbooks": (EvidenceKind.DOC,),
    "Tickets": (EvidenceKind.TICKET,),
}


def build_timeline(inv: Investigation) -> list[TimelineEvent]:
    """The incident's own chain first, then at most ``MAX_TIMELINE_CONTEXT`` related items.

    Observations of the system (changes, rollouts, metric/log anomalies, alerts) are the
    chain of events, in time order; at equal times a change comes before its effects.
    Tickets and runbooks are context: old tickets from the lookback window would otherwise
    sort first and push the real chain down, so only the most relevant ones (the agents
    return them ranked) follow the chain.
    """
    if inv.context is None:
        return []
    start = inv.context.time_range.start - TIMELINE_LOOKBACK
    end = inv.context.time_range.end + timedelta(minutes=5)
    chain: list[tuple[int, TimelineEvent]] = []
    context: list[TimelineEvent] = []
    for r in inv.results:
        if r.status is AgentStatus.FAILED:
            continue
        for e in r.evidence:
            if e.timestamp is None or not start <= e.timestamp <= end:
                continue
            event = TimelineEvent(
                timestamp=e.timestamp,
                description=e.summary[:220],
                source=f"{r.agent}:{e.source}",
                evidence_id=e.id,
            )
            if e.kind in TIMELINE_CONTEXT_KINDS:
                context.append(event)
            else:
                chain.append((CHAIN_ORDER.get(e.kind, len(CHAIN_ORDER)), event))
    chain.sort(key=lambda item: (item[1].timestamp, item[0], item[1].source))
    related = context[:MAX_TIMELINE_CONTEXT]
    return [event for _, event in chain][: MAX_TIMELINE - len(related)] + related


def peak_error_rate(inv: Investigation) -> float | None:
    """The highest measured error ratio of the investigated service (metric evidence of
    the vendor-neutral ``error_rate`` SLI), or None when nothing measured it."""
    service = inv.context.service if inv.context else None
    peaks: list[float] = []
    for result in inv.results:
        if result.status is AgentStatus.FAILED:
            continue
        for evidence in result.evidence:
            data = evidence.data or {}
            if evidence.kind is not EvidenceKind.METRIC or data.get("metric") != "error_rate":
                continue
            per_service = (data.get("services") or {}).get(service) or {}
            for key in ("peak", "current"):
                value = per_service.get(key)
                if isinstance(value, int | float):
                    peaks.append(float(value))
    return max(peaks) if peaks else None


def severity(
    outcome: RCAOutcome,
    rules: SeverityRules | None = None,
    tier: int | None = None,
    error_rate: float | None = None,
) -> Severity:
    """The report severity: ``orchestrator.severity`` rules (see ``SeverityRules``) applied
    to the problem signals, the root cause's confidence, the service's catalog tier and
    its measured peak error ratio."""
    rules = rules or SeverityRules()
    signals = set(outcome.correlation.problem_signals)
    if not signals:
        return "none"
    error_signal = bool(signals & set(rules.error_signals))
    user_impact = error_signal or bool(signals & set(rules.availability_signals))
    users_get_errors = (
        error_rate >= rules.critical_error_rate if error_rate is not None else error_signal
    )
    confident = (
        outcome.root_cause is not None
        and outcome.root_cause.confidence >= rules.critical_min_confidence
    )
    tier_critical = (tier or rules.default_tier) in rules.critical_tiers
    if users_get_errors and confident and tier_critical:
        return "critical"
    if user_impact or rules.critical_alert_signal in signals:
        return "high"
    if signals & set(rules.latency_signals):
        return "medium"
    return "low"


def impact(inv: Investigation) -> str:
    """The metric/log evidence that describes user impact (error rate, latency)."""
    lines: list[str] = []
    for kind in IMPACT_KINDS:
        for result in inv.results:
            problem = set(result.signals) - BENIGN_SIGNALS
            if not problem or result.status is AgentStatus.FAILED:
                continue
            for evidence in result.evidence:
                text = evidence.summary
                if evidence.kind is kind and any(
                    w in text.lower() for w in (" up", "errors in", "error rate", "latency")
                ):
                    lines.append(text.split("; ")[0][:200])
                    break
    return "; ".join(list(dict.fromkeys(lines))[:2])


def affected_services(inv: Investigation, outcome: RCAOutcome) -> list[str]:
    services: list[str] = []
    if inv.context and inv.context.service and outcome.correlation.problem_signals:
        services.append(inv.context.service)
    top = outcome.candidates[0] if outcome.candidates and outcome.root_cause else None
    if top is not None and top.dependency and top.dependency not in services:
        services.append(top.dependency)
    return services


def security_notes(inv: Investigation) -> list[str]:
    """Evidence whose raw data looked like instructions to the model (PR-042): named in
    the report so a human knows the monitored data tried to steer the investigation."""
    notes = []
    for result in inv.results:
        for evidence in result.evidence:
            flagged = evidence.data.get(SUSPECTED_INJECTION)
            if isinstance(flagged, dict) and flagged:
                notes.append(
                    f"Security: suspected prompt injection in {evidence.source} output "
                    f"(`{evidence.id}`, {result.agent} agent): {', '.join(sorted(flagged))}. "
                    "It was treated as data only; review the source."
                )
    return notes


def open_questions(inv: Investigation, outcome: RCAOutcome, missing: Sequence[str]) -> list[str]:
    questions = [f"Not checked / incomplete: {m}" for m in missing]
    questions += security_notes(inv)
    if outcome.root_cause is None and outcome.hypotheses:
        questions.append(
            "Evidence is too weak for a root cause: which additional data would confirm "
            f"'{outcome.hypotheses[0].statement}'?"
        )
    if outcome.root_cause is not None and outcome.root_cause.contradicting_evidence_ids:
        questions.append(
            "Some evidence contradicts the leading hypothesis: "
            + ", ".join(outcome.root_cause.contradicting_evidence_ids)
        )
    for candidate in outcome.candidates[1:3]:
        questions.append(f"Alternative ({candidate.confidence:.0%}): {candidate.statement}")
    return questions


def build_report(
    inv: Investigation,
    outcome: RCAOutcome,
    missing: Sequence[str],
    *,
    rules: SeverityRules | None = None,
    tier: int | None = None,
) -> InvestigationReport:
    ctx = inv.context
    service = (ctx.service if ctx else None) or "the service"
    env = (ctx.environment if ctx else None) or "the environment"
    window = (
        f"{ctx.time_range.start:%Y-%m-%d %H:%M} to {ctx.time_range.end:%H:%M} UTC" if ctx else ""
    )
    agents = sorted({r.agent for r in inv.results if r.status is not AgentStatus.FAILED})
    root = outcome.root_cause
    if outcome.no_incident:
        summary = (
            f"No incident detected for {service} in {env} ({window}): all {len(agents)} "
            "agents that ran report normal behaviour. No root cause to report."
        )
    elif root is None:
        top = outcome.hypotheses[0].statement if outcome.hypotheses else "none"
        summary = (
            f"Abnormal signals on {service} in {env} ({window}) but no root cause identified: "
            f"the evidence is too weak or inconsistent. Weak lead: {top}"
        )
    else:
        candidate = outcome.candidates[0]
        first = outcome.correlation.alignment.first_error
        started = (
            f" First errors at {first.timestamp:%H:%M} UTC." if first and first.timestamp else ""
        )
        summary = (
            f"{root.statement} Confidence {root.confidence:.0%}: {len(candidate.sources)} "
            f"independent sources agree ({', '.join(candidate.sources)}).{started}"
        )
    next_steps = [r.action for r in outcome.recommendations]
    if outcome.no_incident:
        next_steps = ["No action needed; re-run the investigation if symptoms persist."]
    report = InvestigationReport(
        summary=summary,
        root_cause_hypothesis_id=root.id if root else None,
        confidence=root.confidence if root else 0.0,
        impact=impact(inv) if not outcome.no_incident else "No user impact detected.",
        affected_services=affected_services(inv, outcome),
        severity=severity(outcome, rules, tier, peak_error_rate(inv)),
        next_steps=next_steps,
        open_questions=open_questions(inv, outcome, missing),
    )
    return report.model_copy(update={"markdown": render_markdown(inv, report, outcome)})


def _evidence_line(e_id: str, inv: Investigation) -> str:
    for result in inv.results:
        for evidence in result.evidence:
            if evidence.id == e_id:
                link = f" ([link]({evidence.link}))" if evidence.link else ""
                return f"`{e_id}` [{result.agent}] {evidence.summary[:200]}{link}"
    return f"`{e_id}`"


def render_markdown(inv: Investigation, report: InvestigationReport, outcome: RCAOutcome) -> str:
    ctx = inv.context
    lines = [
        f"# Incident report: {inv.incident.title}",
        "",
        f"- **Investigation:** `{inv.id}` ({inv.mode})",
        f"- **Service:** {ctx.service if ctx else '-'} · **Environment:** "
        f"{ctx.environment if ctx else '-'}",
    ]
    if ctx:
        lines.append(
            f"- **Window:** {ctx.time_range.start:%Y-%m-%d %H:%M} to "
            f"{ctx.time_range.end:%Y-%m-%d %H:%M} UTC"
        )
    lines += [
        f"- **Severity:** {report.severity} · **Confidence:** {report.confidence:.0%}",
        "",
        "## Summary",
        "",
        report.summary,
        "",
        "## Incident and impact",
        "",
        report.impact or "-",
        "",
        f"Affected services: {', '.join(report.affected_services) or 'none'}",
        "",
        "## Root cause hypothesis",
        "",
    ]
    if outcome.root_cause is None:
        lines.append("**No root cause identified.**")
    for h in outcome.hypotheses:
        marker = " (root cause)" if outcome.root_cause and h.id == outcome.root_cause.id else ""
        lines += [
            f"### {h.confidence:.0%}{marker}: {h.statement}",
            "",
            "Supporting evidence:",
            *(f"- {_evidence_line(i, inv)}" for i in h.supporting_evidence_ids),
        ]
        if h.contradicting_evidence_ids:
            lines += [
                "",
                "Contradicting evidence:",
                *(f"- {_evidence_line(i, inv)}" for i in h.contradicting_evidence_ids),
            ]
        lines.append("")
    lines += ["## Timeline", ""]
    lines += [
        f"- {t.timestamp:%Y-%m-%d %H:%M:%S} · {t.source} · {t.description}" for t in inv.timeline
    ] or ["- (no timestamped evidence)"]
    lines += ["", "## Key findings", ""]
    for claim in inv.claims or []:
        if claim.kind in (ClaimKind.FACT, ClaimKind.OBSERVATION, ClaimKind.CORRELATION):
            lines.append(
                f"- **{claim.kind.value}** {claim.description} ({', '.join(claim.evidence_ids)})"
            )
    for title, kinds in SECTIONS.items():
        items = [(r.agent, e) for r in inv.results for e in r.evidence if e.kind in kinds]
        if not items:
            continue
        lines += ["", f"## {title}", ""]
        lines += [
            f"- `{e.id}` {e.summary[:200]}" + (f" ([link]({e.link}))" if e.link else "")
            for _, e in items[:8]
        ]
    lines += ["", "## Next steps", ""]
    lines += [f"{i}. {step}" for i, step in enumerate(report.next_steps, start=1)] or ["-"]
    if report.open_questions:
        lines += ["", "## Open questions", ""]
        lines += [f"- {q}" for q in report.open_questions]
    lines += ["", "---", f"Tokens: {inv.usage.total_tokens} · Agents: {len(inv.results)}"]
    return "\n".join(lines).rstrip() + "\n"
