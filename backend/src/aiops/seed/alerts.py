"""Synthetic Alertmanager alerts for scenarios S0-S5 (PR-023).

Until Prometheus evaluates ``deploy/compose/config/prometheus/alert-rules.yml``
(PR-020), the alerts those rules WOULD fire are posted straight to Alertmanager's
API v2, with the same alert names, labels and annotations. The timeline matches
the synthetic logs (``aiops.seed.logs``): the incident starts at now-20m, the S1
rollout happens at now-22m.

Expiry: Alertmanager resolves an alert at its ``endsAt``. Prometheus keeps alerts
alive by re-sending them every evaluation with ``endsAt`` a few minutes ahead. The
seeder has no loop, so firing alerts get ``endsAt = wall-clock now + ttl`` (24h by
default): long enough for a working day, and they disappear on their own afterwards.
``startsAt`` follows the scenario anchor (``--now``), so fixtures recorded at a fixed
time still show the right "firing since". Re-seeding first resolves the previous
seed's alerts (matched by their ``generatorURL`` marker), so scenarios don't mix.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiops.seed.logs import SCENARIOS, SeedWindow

GENERATOR_PREFIX = "aiops-seed://"
DEFAULT_TTL = timedelta(hours=24)
NAMESPACE = {"production": "prod", "staging": "staging"}
TEAMS = {
    "payment-service": "payments",
    "order-service": "commerce",
    "inventory-service": "commerce",
    "user-service": "identity",
    "redis": "platform",
}


@dataclass(frozen=True)
class RuleTemplate:
    """Labels/annotations of one rule in alert-rules.yml (kept in sync by a unit test)."""

    severity: str
    summary: str
    description: str
    runbook: str

    @property
    def runbook_url(self) -> str:
        return f"knowledge-base/runbooks/{self.runbook}"


RULES: dict[str, RuleTemplate] = {
    "HighErrorRate": RuleTemplate(
        "critical",
        "{service}: {value} of requests fail with 5xx",
        "More than 5% of {service} requests in {namespace} returned HTTP 5xx over the last "
        "5 minutes (current {value}).",
        "high-error-rate.md",
    ),
    "HighLatencyP95": RuleTemplate(
        "warning",
        "{service}: p95 latency {value}",
        "The 95th percentile latency of {service} in {namespace} has been above 1s for "
        "5 minutes (current {value}).",
        "high-latency.md",
    ),
    "DatabaseConnectionPoolExhausted": RuleTemplate(
        "critical",
        "{service}: database connection pool exhausted",
        "{service} in {namespace} is using at least 95% of its database connection pool and "
        "requests are waiting for a connection.",
        "database-connection-pool.md",
    ),
    "PodCrashLooping": RuleTemplate(
        "critical",
        "{service}: pod {pod} is crash looping",
        "Pod {namespace}/{pod} restarted {value} times in the last 15 minutes.",
        "pod-crashloop.md",
    ),
    "PodOOMKilled": RuleTemplate(
        "critical",
        "{service}: container OOMKilled in {pod}",
        "A container of {namespace}/{pod} was killed for exceeding its memory limit "
        "(OOMKilled) and restarted in the last 10 minutes.",
        "memory-leak-oom.md",
    ),
    "DeploymentReplicasMismatch": RuleTemplate(
        "warning",
        "{deployment}: available replicas do not match the spec",
        "Deployment {namespace}/{deployment} has had fewer available replicas than desired "
        "for 5 minutes (image pull failures, failing probes, capacity).",
        "bad-deployment-rollback.md",
    ),
    "RedisDown": RuleTemplate(
        "critical",
        "redis: cache is unreachable",
        "The Redis exporter cannot reach Redis (or reports no data) for 1 minute. Services "
        "fall back to the database; expect higher latency everywhere.",
        "redis-outage.md",
    ),
}


@dataclass(frozen=True)
class ScenarioAlert:
    alertname: str
    service: str
    offset: timedelta  # startsAt relative to the incident start (now - 20m)
    value: str = ""
    extra: tuple[tuple[str, str], ...] = ()  # rule-specific labels (pod, deployment)


def _pod(service: str) -> tuple[tuple[str, str], ...]:
    return (("pod", f"{service}-7d9f6b5c4-x2k8p"),)


M = timedelta(minutes=1)

#: What fires in each scenario. Offsets follow the synthetic logs' timeline.
SCENARIO_ALERTS: dict[str, tuple[ScenarioAlert, ...]] = {
    "S0": (),
    "S1": (
        # v1.8.2 rolled out at -2m; the pool saturates first, then the 5xx rate crosses 5%.
        ScenarioAlert("DatabaseConnectionPoolExhausted", "payment-service", 1 * M),
        ScenarioAlert("HighErrorRate", "payment-service", 2 * M, "24.6%"),
    ),
    "S2": (
        # First OOM restart at +3m, then every 4 minutes (see the logs' restart lines).
        ScenarioAlert("HighErrorRate", "order-service", 2 * M, "14.8%"),
        ScenarioAlert("PodOOMKilled", "order-service", 3 * M, extra=_pod("order-service")),
        ScenarioAlert("PodCrashLooping", "order-service", 12 * M, "3", _pod("order-service")),
    ),
    "S3": (
        # inventory-service slows down first, but latency rules wait `for: 5m` while the
        # error-rate rule waits 2m: order-service's HighErrorRate fires first. The order in
        # which alerts fire is not the causal order.
        ScenarioAlert("HighLatencyP95", "inventory-service", 5 * M, "4.1s"),
        ScenarioAlert("HighErrorRate", "order-service", 3 * M, "29.7%"),
        ScenarioAlert("HighLatencyP95", "order-service", 6 * M, "3.05s"),
    ),
    "S4": (
        ScenarioAlert(
            "DeploymentReplicasMismatch",
            "user-service",
            5 * M,
            extra=(("deployment", "user-service"),),
        ),
    ),
    "S5": (
        ScenarioAlert("RedisDown", "redis", 1 * M),
        ScenarioAlert("HighLatencyP95", "payment-service", 6 * M, "1.2s"),
        ScenarioAlert("HighLatencyP95", "order-service", 7 * M, "1.4s"),
    ),
}


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_alert(
    spec: ScenarioAlert,
    scenario: str,
    window: SeedWindow,
    *,
    ends_at: datetime,
    environment: str = "production",
) -> dict[str, Any]:
    """One Alertmanager API v2 ``postableAlert``."""
    rule = RULES[spec.alertname]
    namespace = NAMESPACE[environment]
    extra = dict(spec.extra)
    fmt = {"service": spec.service, "namespace": namespace, "value": spec.value, **extra}
    labels = {
        "alertname": spec.alertname,
        "service": spec.service,
        "severity": rule.severity,
        "team": TEAMS.get(spec.service, "platform"),
        "namespace": namespace,
        **extra,
    }
    return {
        "labels": labels,
        "annotations": {
            "summary": rule.summary.format(**fmt),
            "description": rule.description.format(**fmt),
            "runbook_url": rule.runbook_url,
        },
        "startsAt": iso(window.incident_start + spec.offset),
        "endsAt": iso(ends_at),
        "generatorURL": f"{GENERATOR_PREFIX}{scenario}/{spec.alertname}",
    }


def scenario_alerts(
    scenario: str,
    window: SeedWindow,
    *,
    wall_now: datetime | None = None,
    ttl: timedelta = DEFAULT_TTL,
    environment: str = "production",
) -> list[dict[str, Any]]:
    """Firing alerts of a scenario; they stay active until ``wall_now + ttl``."""
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario '{scenario}' (known: {', '.join(SCENARIOS)})")
    if environment not in NAMESPACE:
        raise ValueError(f"environment must be one of {sorted(NAMESPACE)}")
    ends_at = (wall_now or datetime.now(UTC)) + ttl
    return [
        build_alert(spec, scenario, window, ends_at=ends_at, environment=environment)
        for spec in SCENARIO_ALERTS[scenario]
    ]


def resolved(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Payloads that resolve ``alerts`` (as returned by GET /api/v2/alerts).

    ``endsAt == startsAt`` on purpose: Alertmanager merges a new alert into a stored one
    with the same labels when their time ranges overlap, and an explicitly resolved
    ``endsAt`` wins that merge. A zero-length range never overlaps, so re-seeding the
    same alert (with any ``--now``) always starts fresh instead of staying resolved.
    """
    return [
        {
            "labels": a["labels"],
            "annotations": a.get("annotations", {}),
            "startsAt": a["startsAt"],
            "endsAt": a["startsAt"],
            "generatorURL": a.get("generatorURL", ""),
        }
        for a in alerts
    ]
