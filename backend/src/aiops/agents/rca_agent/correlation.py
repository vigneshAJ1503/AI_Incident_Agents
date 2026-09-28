"""Deterministic correlation (PR-033): signals x agents -> scored candidate hypotheses.

No model, no vendor: rules key on our own signal vocabulary, emitted by any agent.

* **Co-occurrence matrix**: which problem signals were raised by which agents, and which
  signal pairs co-occur across *different* agents (independent corroboration).
* **Time alignment**: the latest change (commit, release, rollout) before the first
  error, and the first alert, from evidence timestamps.
* **Rules** turn matching signals into candidate hypotheses. Confidence grows with the
  number of independent data sources (logs, metrics, alerts, k8s, code); context sources
  (runbooks, tickets) add a little; contradicting signals subtract. Fewer than 2
  independent sources can never exceed 0.7 (MASTER_PLAN §12).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import combinations
from typing import Literal

from aiops.core.models import AgentResult, AgentStatus, Evidence, EvidenceKind
from aiops.core.signals import BENIGN_SIGNALS

#: Evidence kinds that are observations of the system (independent sources). Docs and
#: tickets are context: they corroborate, they don't observe.
CONTEXT_KINDS = frozenset({EvidenceKind.DOC, EvidenceKind.TICKET})
CHANGE_KINDS = frozenset({EvidenceKind.COMMIT, EvidenceKind.K8S_EVENT})
ALIGNMENT_WINDOW = timedelta(hours=6)
MIN_SOURCES_FOR_HIGH_CONFIDENCE = 2
LOW_SOURCE_CAP = 0.55
SOURCE_CONFIDENCE = {0: 0.0, 1: 0.4, 2: 0.62, 3: 0.74, 4: 0.82, 5: 0.88}
CONTEXT_BONUS = 0.03
ALIGNMENT_BONUS = 0.04
CONTRADICTION_PENALTY = 0.12
MAX_CONFIDENCE = 0.95

ERROR_SIGNALS = frozenset({"error_rate_up", "new_error_pattern", "latency_up", "alerts_firing"})
CHANGE_SIGNALS = frozenset(
    {"recent_rollout", "recent_deployment_change", "risky_config_change", "deployment_detected"}
)
#: Every rule is weakened by an explicit all-clear from an observing agent.
ALL_CLEAR = frozenset({"no_anomaly", "no_active_alerts", "healthy"})

_VERSION = re.compile(r"\bv\d+\.\d+\.\d+\b")
_VERSION_BUMP = re.compile(r"\b(v\d+\.\d+\.\d+)\s*->\s*(v\d+\.\d+\.\d+)\b")
_CONFIG_DIFF = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\s*'?([^'\s]*)'?\s*->\s*'?([^'\s,;)]*)'?")


@dataclass(frozen=True)
class Action:
    action: str
    rationale: str
    risk: Literal["low", "medium", "high"] = "low"
    requires_approval: bool = False


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    #: At least one trigger signal must be present for the rule to fire.
    triggers: frozenset[str]
    support: frozenset[str]
    contradict: frozenset[str]
    statement: str  # {service} {dependency} {change}
    target: Literal["incident", "dependency"] = "incident"
    is_change: bool = False  # a change-driven hypothesis: time alignment matters
    weight: float = 1.0  # generic rules rank below specific ones
    actions: tuple[Action, ...] = ()


ROLLBACK = Action(
    "Roll back {service} to the previous release (or revert the change)",
    "The change is the most likely trigger; rolling back is the fastest mitigation.",
    risk="medium",
    requires_approval=True,
)

RULES: tuple[Rule, ...] = (
    Rule(
        id="db_pool_misconfiguration",
        title="Database connection pool misconfiguration",
        triggers=frozenset({"db_timeout_errors_up", "db_pool_saturated"}),
        support=frozenset(
            {
                "db_timeout_errors_up",
                "db_pool_saturated",
                "risky_config_change",
                "recent_rollout",
                "recent_deployment_change",
                "deployment_detected",
                "critical_alert_firing",
                "error_rate_up",
                "latency_up",
                "known_issue_open",
                "known_issue_documented",
            }
        ),
        contradict=frozenset({"cache_down", "oom_killed", "image_pull_error"}),
        statement=(
            "Database connection pool misconfiguration introduced by a recent change to "
            "{service}{change} causes connection timeouts and HTTP 500s."
        ),
        is_change=True,
        actions=(
            ROLLBACK,
            Action(
                "Restore the previous DB pool size in {service}'s configuration",
                "Requests wait for a pooled connection and time out.",
                risk="medium",
                requires_approval=True,
            ),
        ),
    ),
    Rule(
        id="memory_leak_oom",
        title="Memory leak / OOM kills",
        triggers=frozenset({"oom_errors", "oom_killed", "memory_pressure"}),
        support=frozenset(
            {
                "oom_errors",
                "oom_killed",
                "memory_pressure",
                "pod_restarts",
                "crash_loop",
                "service_restarts",
                "risky_config_change",
                "critical_alert_firing",
                "traffic_drop",
                "error_rate_up",
                "known_issue_documented",
            }
        ),
        contradict=frozenset({"image_pull_error", "cache_down", "db_pool_saturated"}),
        statement=(
            "Memory leak in {service} causes OutOfMemoryError and repeated container "
            "restarts (OOMKilled){change}."
        ),
        is_change=True,
        actions=(
            ROLLBACK,
            Action(
                "Raise {service}'s memory limit temporarily and capture a heap dump",
                "Buys time while the leak is fixed; the heap dump shows what grows.",
                risk="medium",
                requires_approval=True,
            ),
        ),
    ),
    Rule(
        id="slow_dependency",
        title="Slow downstream dependency",
        triggers=frozenset({"dependency_timeouts", "dependency_latency_up"}),
        support=frozenset(
            {
                "dependency_timeouts",
                "dependency_latency_up",
                "dependency_alert_firing",
                "dependency_service_change",
                "dependency_rollout",
                "dependency_known_issue_open",
                "latency_up",
                "error_rate_up",
                "known_issue_documented",
            }
        ),
        contradict=frozenset({"cache_down", "cache_connection_errors", "oom_killed"}),
        statement=(
            "Slow downstream dependency {dependency}{change} causes {service} upstream timeouts."
        ),
        target="dependency",
        actions=(
            Action(
                "Investigate and mitigate {dependency} latency (roll back its recent change)",
                "{service} times out waiting for {dependency}.",
                risk="medium",
                requires_approval=True,
            ),
            Action(
                "Tighten {service}'s timeout/circuit breaker towards {dependency}",
                "Fail fast instead of piling up requests.",
            ),
        ),
    ),
    Rule(
        id="bad_deployment_image",
        title="Bad deployment: image cannot be pulled",
        triggers=frozenset({"image_pull_error", "unreleased_image_tag"}),
        support=frozenset(
            {
                "image_pull_error",
                "unreleased_image_tag",
                "image_tag_change",
                "replicas_unavailable",
                "recent_rollout",
                "recent_deployment_change",
                "capacity_degraded",
                "alerts_firing",
                "traffic_drop",
                "known_issue_documented",
            }
        ),
        contradict=frozenset({"oom_killed", "cache_down", "db_pool_saturated"}),
        statement=(
            "{service} deployment{change} references an image tag that cannot be pulled "
            "(ImagePullBackOff), leaving it with fewer ready replicas."
        ),
        is_change=True,
        actions=(
            Action(
                "Roll back {service}'s deployment to the last working revision",
                "The new pods can't start; the old revision still serves traffic.",
                risk="medium",
                requires_approval=True,
            ),
            Action(
                "Publish the missing image tag or fix the tag in the manifest",
                "The referenced tag has no release/image.",
            ),
        ),
    ),
    Rule(
        id="cache_outage",
        title="Cache outage",
        triggers=frozenset({"cache_down", "cache_connection_errors"}),
        support=frozenset(
            {
                "cache_down",
                "cache_connection_errors",
                "dependency_unavailable",
                "dependency_alert_firing",
                "dependency_latency_up",
                "latency_up",
                "similar_past_incident",
                "known_issue_documented",
            }
        ),
        contradict=frozenset({"oom_killed", "image_pull_error", "db_pool_saturated"}),
        statement=(
            "Cache outage{dependency_paren} forces fallback to the database, increasing latency "
            "for {service} and every service that uses the cache."
        ),
        target="dependency",
        actions=(
            Action(
                "Restore the cache{dependency_paren} (scale it back up / restart it)",
                "Every cache miss falls back to the database.",
                risk="medium",
                requires_approval=True,
            ),
        ),
    ),
    Rule(
        id="recent_change_regression",
        title="Regression after a recent change",
        triggers=frozenset({"recent_rollout", "recent_deployment_change", "risky_config_change"}),
        support=frozenset(CHANGE_SIGNALS | ERROR_SIGNALS),
        contradict=frozenset({"no_recent_changes"}),
        statement="A recent change to {service}{change} correlates with the errors.",
        is_change=True,
        weight=0.8,
        actions=(ROLLBACK,),
    ),
)


# --------------------------------------------------------------------------- analysis


@dataclass
class TimeAlignment:
    first_error: Evidence | None = None
    first_alert: Evidence | None = None
    change: Evidence | None = None  # latest change before the first error

    @property
    def aligned(self) -> bool:
        if self.change is None or self.first_error is None:
            return False
        if self.change.timestamp is None or self.first_error.timestamp is None:
            return False
        gap = self.first_error.timestamp - self.change.timestamp
        return timedelta(0) <= gap <= ALIGNMENT_WINDOW

    def lead(self) -> timedelta | None:
        if not self.aligned or self.change is None or self.first_error is None:
            return None
        assert self.change.timestamp is not None and self.first_error.timestamp is not None  # noqa: S101
        return self.first_error.timestamp - self.change.timestamp


@dataclass
class Candidate:
    rule: Rule
    confidence: float
    sources: list[str]  # independent (observing) agents
    context: list[str]  # corroborating context agents
    contradicting: list[str]  # agents whose signals contradict
    signals: list[str]
    supporting_evidence: list[str]
    contradicting_evidence: list[str]
    statement: str
    dependency: str | None = None


@dataclass
class Correlation:
    matrix: dict[str, list[str]] = field(default_factory=dict)  # signal -> agents
    pairs: list[tuple[str, str, str, str]] = field(default_factory=list)  # (s1, a1, s2, a2)
    alignment: TimeAlignment = field(default_factory=TimeAlignment)
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def problem_signals(self) -> list[str]:
        return list(self.matrix)


def _is_context(result: AgentResult) -> bool:
    kinds = {e.kind for e in result.evidence}
    return bool(kinds) and kinds <= CONTEXT_KINDS


def usable(results: Sequence[AgentResult]) -> list[AgentResult]:
    return [r for r in results if r.status is not AgentStatus.FAILED]


def cooccurrence(
    results: Sequence[AgentResult],
) -> tuple[dict[str, list[str]], list[tuple[str, str, str, str]]]:
    matrix: dict[str, list[str]] = {}
    for result in usable(results):
        for signal in result.signals:
            if signal in BENIGN_SIGNALS:
                continue
            agents = matrix.setdefault(signal, [])
            if result.agent not in agents:
                agents.append(result.agent)
    pairs = []
    flat = [(s, a) for s, agents in matrix.items() for a in agents]
    for (s1, a1), (s2, a2) in combinations(flat, 2):
        if a1 != a2 and s1 != s2:
            pairs.append((s1, a1, s2, a2))
    return matrix, pairs


def key_evidence(result: AgentResult, limit: int = 3) -> list[str]:
    """The evidence ids that best represent a result (cited by findings first)."""
    ids: list[str] = []
    for finding in result.findings:
        if finding.type == "overview":
            continue
        ids.extend(i for i in finding.evidence_ids if i not in ids)
    ids.extend(e.id for e in result.evidence if e.timestamp is not None and e.id not in ids)
    ids.extend(e.id for e in result.evidence if e.id not in ids)
    return ids[:limit]


def align(results: Sequence[AgentResult], window_start: datetime | None = None) -> TimeAlignment:
    errors: list[Evidence] = []
    alerts: list[Evidence] = []
    changes: list[Evidence] = []
    for result in usable(results):
        problem = [s for s in result.signals if s not in BENIGN_SIGNALS]
        for evidence in result.evidence:
            if evidence.timestamp is None:
                continue
            if evidence.kind is EvidenceKind.ALERT:
                alerts.append(evidence)
            elif evidence.kind in CHANGE_KINDS:
                changes.append(evidence)
            elif evidence.kind in (EvidenceKind.LOG, EvidenceKind.METRIC) and problem:
                errors.append(evidence)
    alignment = TimeAlignment(
        first_error=min(errors, key=_ts) if errors else None,
        first_alert=min(alerts, key=_ts) if alerts else None,
    )
    anchor = alignment.first_error.timestamp if alignment.first_error else None
    if anchor is not None:
        before = [c for c in changes if c.timestamp is not None and c.timestamp <= anchor]
        if before:
            alignment.change = max(before, key=_ts)
    return alignment


def _ts(evidence: Evidence) -> datetime:
    assert evidence.timestamp is not None  # noqa: S101 (filtered by the callers)
    return evidence.timestamp


def change_detail(results: Sequence[AgentResult], alignment: TimeAlignment) -> str:
    """' (v1.8.2, DB_POOL_SIZE 20 -> 2)' from change evidence; '' when nothing concrete."""
    texts: list[str] = []
    if alignment.change is not None:
        texts.append(alignment.change.summary)
    for result in usable(results):
        if set(result.signals) & (CHANGE_SIGNALS | {"image_tag_change", "unreleased_image_tag"}):
            texts.extend(
                e.summary for e in result.evidence if e.kind in CHANGE_KINDS and e.timestamp
            )
    # "v3.1.4 -> v3.2.0" names the new version; otherwise the first version mentioned.
    bumps = [m.group(2) for text in texts if (m := _VERSION_BUMP.search(text))]
    plain = [m.group(0) for text in texts if (m := _VERSION.search(text))]
    version = (bumps or plain or [None])[0]
    config = None
    for text in texts:
        match = _CONFIG_DIFF.search(text)
        if match and match.group(2) != match.group(3):
            config = f"{match.group(1)} {match.group(2)} -> {match.group(3)}"
            break
    parts = [p for p in (version, config) if p]
    return f" ({', '.join(parts)})" if parts else ""


def _dependency(
    results: Sequence[AgentResult], rule: Rule, candidates: Iterable[str]
) -> str | None:
    """The catalog dependency most mentioned by the results that raised the rule's signals
    (trigger results weigh 3x)."""
    scores: dict[str, int] = {}
    for result in usable(results):
        hits = set(result.signals)
        weight = 3 if hits & rule.triggers else 1 if hits & rule.support else 0
        if not weight:
            continue
        text = " ".join([result.summary, *(e.summary for e in result.evidence)]).casefold()
        for name in candidates:
            count = text.count(name.casefold())
            if count:
                scores[name] = scores.get(name, 0) + weight * count
    return max(scores, key=lambda n: scores[n]) if scores else None


def correlate(
    results: Sequence[AgentResult],
    *,
    service: str,
    dependencies: Sequence[str] = (),
) -> Correlation:
    matrix, pairs = cooccurrence(results)
    alignment = align(results)
    correlation = Correlation(matrix=matrix, pairs=pairs, alignment=alignment)
    present = set(matrix)
    if not present:
        return correlation
    detail = change_detail(results, alignment)
    for rule in RULES:
        if not present & rule.triggers:
            continue
        sources: list[str] = []
        context: list[str] = []
        supporting: list[str] = []
        signals: list[str] = []
        for result in usable(results):
            hits = [s for s in result.signals if s in rule.support or s in rule.triggers]
            if not hits:
                continue
            signals.extend(s for s in hits if s not in signals)
            (context if _is_context(result) else sources).append(result.agent)
            supporting.extend(i for i in key_evidence(result) if i not in supporting)
        contradicting_agents: list[str] = []
        contradicting: list[str] = []
        for result in usable(results):
            against = set(result.signals) & (rule.contradict | ALL_CLEAR)
            if rule.is_change and "no_recent_changes" in result.signals:
                against.add("no_recent_changes")
            # "healthy" from k8s is expected when the problem is elsewhere
            if against == {"healthy"} and rule.id != "bad_deployment_image":
                continue
            if against and result.agent not in sources:
                contradicting_agents.append(result.agent)
                contradicting.extend(key_evidence(result, limit=2))
        if not supporting:
            continue
        aligned = rule.is_change and alignment.aligned
        confidence = SOURCE_CONFIDENCE[min(len(sources), 5)]
        confidence += CONTEXT_BONUS * len(context) + (ALIGNMENT_BONUS if aligned else 0.0)
        confidence -= CONTRADICTION_PENALTY * len(contradicting_agents)
        if len(sources) < MIN_SOURCES_FOR_HIGH_CONFIDENCE:
            confidence = min(confidence, LOW_SOURCE_CAP)
        confidence = round(max(0.0, min(MAX_CONFIDENCE, confidence * rule.weight)), 2)
        dependency = (
            _dependency(results, rule, dependencies) if rule.target == "dependency" else None
        )
        statement = rule.statement.format(
            service=service,
            dependency=dependency or "a downstream dependency",
            dependency_paren=f" ({dependency})" if dependency else "",
            change=detail if rule.is_change or rule.target == "dependency" else "",
        )
        correlation.candidates.append(
            Candidate(
                rule=rule,
                confidence=confidence,
                sources=sources,
                context=context,
                contradicting=contradicting_agents,
                signals=signals,
                supporting_evidence=supporting,
                contradicting_evidence=contradicting,
                statement=statement.replace(" )", ")"),
                dependency=dependency,
            )
        )
    correlation.candidates.sort(key=lambda c: (-c.confidence, -c.rule.weight, c.rule.id))
    return correlation
