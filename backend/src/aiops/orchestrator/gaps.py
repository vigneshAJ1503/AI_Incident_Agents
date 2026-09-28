"""Gap analysis (PR-031, MASTER_PLAN §12 step 3): round-1 signals -> round-2 follow-ups.

Deterministic, no LLM: a signal -> follow-up table (defaults below, extended or
overridden by ``orchestrator.followups`` in the profile). Signals are our own
vendor-neutral vocabulary (``db_timeout_errors_up``, ``dependency_timeouts``, ...), so
the table is the same for every company.

* ``target: incident``   -> re-check the investigated service with a sharper objective
  (skipped when round 1 already ran that agent successfully on it: no duplicate work).
* ``target: dependency`` -> the same agent on the catalog dependency that round-1
  evidence names (e.g. the slow downstream service in "Timeout calling
  inventory-service ...").
* ``keywords`` feed the knowledge agent's search (``hints.keywords``).

Round-2 context agents (``orchestrator.round2_agents``) get the round-1 findings:
knowledge -> ``hints.signals/patterns/alerts/keywords``; tickets -> round-1 signals as
``context.symptoms``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from aiops.core.catalog import ServiceCatalog
from aiops.core.config import FollowupRule
from aiops.core.models import AgentResult, AgentStatus
from aiops.core.signals import BENIGN_SIGNALS

DEFAULT_FOLLOWUPS: dict[str, list[FollowupRule]] = {
    "db_timeout_errors_up": [
        FollowupRule(agent="code", objective="Diff the database/pool configuration"),
        FollowupRule(agent="metrics", objective="Check DB connection pool saturation"),
        FollowupRule(agent="knowledge", objective="", keywords=["connection pool"]),
    ],
    "dependency_timeouts": [
        FollowupRule(agent="logs", objective="Errors on the slow dependency", target="dependency"),
        FollowupRule(
            agent="metrics", objective="Latency of the slow dependency", target="dependency"
        ),
        FollowupRule(agent="knowledge", objective="", keywords=["dependency timeout"]),
    ],
    "oom_errors": [
        FollowupRule(agent="k8s", objective="Confirm OOMKilled terminations and restarts"),
        FollowupRule(agent="knowledge", objective="", keywords=["memory leak"]),
    ],
    "cache_connection_errors": [
        FollowupRule(agent="k8s", objective="Check the cache workload", target="dependency"),
        FollowupRule(agent="knowledge", objective="", keywords=["redis outage"]),
    ],
    "capacity_degraded": [
        FollowupRule(agent="k8s", objective="Check rollout, image pulls and ready replicas"),
        FollowupRule(agent="knowledge", objective="", keywords=["rollback"]),
    ],
}


@dataclass
class Followup:
    agent: str
    service: str
    objective: str
    reason: str  # the signal that triggered it


@dataclass
class GapAnalysis:
    signals: list[str] = field(default_factory=list)  # non-benign round-1 signals, ordered
    followups: list[Followup] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # human-readable dedup notes
    keywords: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    def knowledge_hints(self) -> dict[str, Any]:
        hints: dict[str, Any] = {"signals": self.signals, "patterns": self.patterns}
        if self.alerts:
            hints["alerts"] = self.alerts
        if self.keywords:
            hints["keywords"] = self.keywords
        return hints


def _ordered(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def problem_signals(results: Sequence[AgentResult]) -> list[str]:
    return _ordered(
        s
        for r in results
        if r.status is not AgentStatus.FAILED
        for s in r.signals
        if s not in BENIGN_SIGNALS
    )


def harvest(results: Sequence[AgentResult], key: str) -> list[str]:
    """Structured hints agents publish in evidence data (``patterns``, ``alerts``)."""
    values: list[str] = []
    for result in results:
        for evidence in result.evidence:
            raw = evidence.data.get(key)
            if isinstance(raw, list):
                values.extend(str(v) for v in raw if isinstance(v, str) and v.strip())
    return _ordered(values)


def named_dependencies(
    results: Sequence[AgentResult], service: str, catalog: ServiceCatalog
) -> list[str]:
    """Catalog dependencies of ``service`` that round-1 findings mention by name."""
    deps = catalog.get(service).depends_on
    texts = " ".join(
        [r.summary for r in results]
        + [e.summary for r in results for e in r.evidence]
        + harvest(results, "patterns")
    ).casefold()
    mentioned = []
    for dep in deps:
        entry = catalog.resolve(dep).service
        names = {dep, *(entry.aliases if entry else [])}
        if any(n.casefold() in texts for n in names if len(n) > 3):
            mentioned.append(dep)
    return mentioned


def analyze_gaps(
    results: Sequence[AgentResult],
    *,
    service: str,
    catalog: ServiceCatalog,
    enabled_agents: set[str],
    round2_agents: set[str],
    overrides: dict[str, list[FollowupRule]] | None = None,
) -> GapAnalysis:
    rules = {**DEFAULT_FOLLOWUPS, **(overrides or {})}
    analysis = GapAnalysis(
        signals=problem_signals(results),
        patterns=harvest(results, "patterns"),
        alerts=harvest(results, "alerts"),
    )
    succeeded = {
        r.agent for r in results if r.status in (AgentStatus.SUCCESS, AgentStatus.NO_SIGNAL)
    }
    seen: set[tuple[str, str]] = set()
    for signal in analysis.signals:
        for rule in rules.get(signal, []):
            if rule.agent not in enabled_agents:
                continue
            analysis.keywords.extend(k for k in rule.keywords if k not in analysis.keywords)
            if rule.agent in round2_agents:
                continue  # already a round-2 context step; keywords feed its hints
            if rule.target == "incident":
                targets = [service]
            else:  # the dependency named by the results that raised this signal
                owners = [r for r in results if signal in r.signals]
                targets = [
                    d for d in named_dependencies(owners, service, catalog) if _known(d, catalog)
                ]
            for target in targets:
                key = (rule.agent, target)
                if key in seen:
                    continue
                seen.add(key)
                if target == service and rule.agent in succeeded:
                    analysis.skipped.append(
                        f"{rule.agent} on {target} ({signal}): already covered by round 1"
                    )
                    continue
                analysis.followups.append(
                    Followup(
                        agent=rule.agent,
                        service=target,
                        objective=rule.objective or f"Follow up on {signal}",
                        reason=signal,
                    )
                )
    return analysis


def _known(name: str, catalog: ServiceCatalog) -> bool:
    return catalog.resolve(name).service is not None
