"""PromQL library for the Metrics agent, built entirely from capability settings.

Metric names and label names come from ``capabilities.metrics.settings`` (portability);
label values (which service, which namespace) come from the service catalog. Each query
returns one series per service (grouped by the service label, or by the kube-state-metrics
app label for pod-level metrics), so the service and its catalog dependencies are covered
by a single ``query_range`` call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

Unit = Literal["ratio", "seconds", "rps", "bytes", "bool", "count"]

DEFAULT_LABELS = {
    "service": "service",
    "namespace": "namespace",
    "status": "status",
    "pod": "pod",
    "k8s_app": "label_app",
}
DEFAULT_METRICS = {
    "requests": "http_requests_total",
    "latency_histogram": "http_request_duration_seconds",
    "db_pool_active": "db_pool_connections_active",
    "db_pool_max": "db_pool_connections_max",
    "db_pool_pending": "db_pool_connections_pending",
    "cache_up": "redis_up",
    "memory_rss": "process_resident_memory_bytes",
    "restarts": "kube_pod_container_status_restarts_total",
    "last_terminated_reason": "kube_pod_container_status_last_terminated_reason",
    "pod_labels": "kube_pod_labels",
}


@dataclass(frozen=True)
class MetricQuery:
    key: str  # error_rate, latency_p95, ...
    title: str
    query: str
    unit: Unit
    group_label: str  # result label that names the service
    panel: str | None  # key into settings.panels (Grafana deep link), if any


def label_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


_REGEX_META = re.compile(r"([.^$*+?{}\[\]\\|()])")


def regex_alternation(values: list[str]) -> str:
    """A quoted RE2 alternation matching exactly ``values`` (metacharacters escaped)."""
    return label_value("|".join(_REGEX_META.sub(r"\\\1", v) for v in values))


@dataclass(frozen=True)
class PromQLLibrary:
    labels: dict[str, str]
    metrics: dict[str, str]
    error_status_regex: str
    rate_window: str

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> PromQLLibrary:
        return cls(
            labels={**DEFAULT_LABELS, **(settings.get("labels") or {})},
            metrics={**DEFAULT_METRICS, **(settings.get("metrics") or {})},
            error_status_regex=str(settings.get("error_status_regex", "5..")),
            rate_window=str(settings.get("rate_window", "2m")),
        )

    def selector(self, services: list[str], extra: dict[str, str]) -> str:
        """``service=~"a|b",namespace="prod"``: the services plus shared label filters."""
        parts = [f"{self.labels['service']}=~{regex_alternation(services)}"]
        parts += [f"{k}={label_value(v)}" for k, v in sorted(extra.items())]
        return ",".join(parts)

    def queries(
        self, services: list[str], extra: dict[str, str], k8s_apps: list[str]
    ) -> list[MetricQuery]:
        m, lb, rw = self.metrics, self.labels, self.rate_window
        svc = lb["service"]
        sel = self.selector(services, extra)
        errors = f"{sel},{lb['status']}=~{label_value(self.error_status_regex)}"
        total = f"sum by ({svc}) (rate({m['requests']}{{{sel}}}[{rw}]))"
        hist = f"{m['latency_histogram']}_bucket{{{sel}}}"

        def quantile(q: float) -> str:
            return f"histogram_quantile({q}, sum by ({svc}, le) (rate({hist}[{rw}])))"

        out = [
            MetricQuery("rps", "Requests per second", total, "rps", svc, "rps"),
            MetricQuery(
                "error_rate",
                "5xx error ratio",
                # "or ... * 0": a service with no 5xx at all has a ratio of 0, not no data.
                f"(sum by ({svc}) (rate({m['requests']}{{{errors}}}[{rw}])) or {total} * 0)"
                f" / {total}",
                "ratio",
                svc,
                "error_rate",
            ),
            MetricQuery("latency_p95", "p95 latency", quantile(0.95), "seconds", svc, "latency"),
            MetricQuery("latency_p99", "p99 latency", quantile(0.99), "seconds", svc, "latency"),
            MetricQuery(
                "db_pool_utilization",
                "DB pool utilisation (active / max)",
                f"sum by ({svc}) ({m['db_pool_active']}{{{sel}}})"
                f" / sum by ({svc}) ({m['db_pool_max']}{{{sel}}})",
                "ratio",
                svc,
                "db_pool",
            ),
            MetricQuery(
                "db_pool_pending",
                "Requests waiting for a DB connection",
                f"sum by ({svc}) ({m['db_pool_pending']}{{{sel}}})",
                "count",
                svc,
                "db_pool",
            ),
            MetricQuery(
                "cache_up",
                "Cache reachable (1 = up)",
                f"min by ({svc}) ({m['cache_up']}{{{sel}}})",
                "bool",
                svc,
                "cache",
            ),
            MetricQuery(
                "memory_rss",
                "Process memory (RSS)",
                f"max by ({svc}) ({m['memory_rss']}{{{sel}}})",
                "bytes",
                svc,
                "memory",
            ),
        ]
        if k8s_apps:
            app = lb["k8s_app"]
            ns = {k: v for k, v in extra.items() if k == lb["namespace"]}
            pod_sel = ",".join(f"{k}={label_value(v)}" for k, v in sorted(ns.items()))
            join = (
                f"* on ({lb['namespace']}, {lb['pod']}) group_left ({app}) "
                f"{m['pod_labels']}{{{app}=~{regex_alternation(k8s_apps)}}}"
            )
            out += [
                MetricQuery(
                    "restarts",
                    "Container restarts (highest count among the pods)",
                    f"max by ({app}) ({m['restarts']}{{{pod_sel}}} {join})",
                    "count",
                    app,
                    None,
                ),
                MetricQuery(
                    "oom_killed",
                    "Last termination was OOMKilled (1 = yes)",
                    f"max by ({app}) ({m['last_terminated_reason']}"
                    f'{{{pod_sel}{"," if pod_sel else ""}reason="OOMKilled"}} {join})',
                    "bool",
                    app,
                    None,
                ),
            ]
        return out
