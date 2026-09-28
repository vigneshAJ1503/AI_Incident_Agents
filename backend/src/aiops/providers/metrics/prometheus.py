"""``metrics/prometheus``: PromQL via prometheus-mcp's ``query_range``, Grafana/Prometheus links.

Works with any Prometheus-compatible query API: Prometheus, Thanos, Cortex/Mimir, Grafana
Cloud, VictoriaMetrics. The backend is chosen by the MCP server's environment
(``METRICS_PROM_URL``, ``METRICS_PROM_BEARER_TOKEN`` or ``METRICS_PROM_USERNAME``/
``METRICS_PROM_PASSWORD``, ``METRICS_PROM_ORG_ID`` for multi-tenant Mimir/Cortex), not by
this adapter.

Metric and label names come from ``capabilities.metrics.settings`` (defaults below);
label values (which services, which namespace) from the service catalog. Each query
returns one series per service (grouped by the service label, or by the
kube-state-metrics app label for pod-level SLIs), so the service and its catalog
dependencies are covered by a single ``query_range`` call.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, ClassVar

from aiops.core.links import format_link, query_link, window_values
from aiops.providers.base import ToolRequest
from aiops.providers.metrics import (
    SLI,
    MetricRequest,
    MetricScope,
    MetricsProvider,
    MetricWindow,
    Values,
    iso_seconds,
)
from aiops.providers.registry import PROVIDER_REGISTRY

QUERY_RANGE = "query_range"
QUERY = "query"

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
DEFAULT_STEP_S = 30
#: Stay well inside prometheus-mcp's points-per-series cap.
MAX_POINTS = 1000


def label_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


_REGEX_META = re.compile(r"([.^$*+?{}\[\]\\|()])")


def regex_alternation(values: list[str]) -> str:
    """A quoted RE2 alternation matching exactly ``values`` (metacharacters escaped)."""
    return label_value("|".join(_REGEX_META.sub(r"\\\1", v) for v in values))


def parse_step(value: Any) -> int:
    """``30s`` / ``1m`` / ``30`` -> seconds (default 30)."""
    text = str(value or "").strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    if text and text[-1] in units and text[:-1].isdigit():
        return int(text[:-1]) * units[text[-1]]
    return int(text) if text.isdigit() else DEFAULT_STEP_S


def parse_series(data: Any, group_label: str) -> dict[str, Values]:
    """prometheus-mcp query_range result -> {label value: [(ts, value)]}."""
    out: dict[str, Values] = {}
    series = data.get("series", []) if isinstance(data, dict) else []
    for s in series:
        if not isinstance(s, dict):
            continue
        key = (s.get("labels") or {}).get(group_label)
        if not key:
            continue
        values: Values = []
        for point in s.get("values") or []:
            if not isinstance(point, list | tuple) or len(point) != 2:
                continue
            ts, raw = point
            value = float(raw) if isinstance(raw, int | float) and math.isfinite(raw) else None
            values.append((float(ts), value))
        out[str(key)] = values
    return out


class PrometheusMetrics(MetricsProvider):
    name: ClassVar[str] = "prometheus"
    mcp: ClassVar[str] = "mcp-servers/prometheus-mcp"
    agent_tools: ClassVar[tuple[str, ...]] = (QUERY, QUERY_RANGE)
    note: ClassVar[str] = (
        "PromQL; any Prometheus-compatible API (Thanos, Mimir, Grafana Cloud, VictoriaMetrics)"
    )
    prompt_fragment: ClassVar[str | None] = "providers/metrics/prometheus"
    default_labels: ClassVar[Mapping[str, str]] = DEFAULT_LABELS

    # -- settings -------------------------------------------------------------------------

    def metrics(self) -> dict[str, str]:
        return {**DEFAULT_METRICS, **(self.settings.get("metrics") or {})}

    @property
    def error_status_regex(self) -> str:
        return str(self.settings.get("error_status_regex", "5.."))

    @property
    def rate_window(self) -> str:
        return str(self.settings.get("rate_window", "2m"))

    def step_seconds(self, window: MetricWindow) -> int:
        """``settings.step``, coarser when the span would exceed ``MAX_POINTS``."""
        span = (window.end - window.start).total_seconds()
        return max(
            parse_step(self.settings.get("step", DEFAULT_STEP_S)), math.ceil(span / MAX_POINTS)
        )

    # -- PromQL ---------------------------------------------------------------------------

    def selector(self, services: list[str], filters: Mapping[str, str]) -> str:
        """``service=~"a|b",namespace="prod"``: the services plus shared label filters."""
        parts = [f"{self.label('service')}=~{regex_alternation(services)}"]
        parts += [f"{k}={label_value(v)}" for k, v in sorted(filters.items())]
        return ",".join(parts)

    def promql(self, sli: SLI, scope: MetricScope) -> tuple[str, str] | None:
        """(PromQL, group label) for ``sli``; None when it can't be asked."""
        m, lb, rw = self.metrics(), self.labels(), self.rate_window
        svc = lb["service"]
        sel = self.selector(scope.services, scope.filters)
        total = f"sum by ({svc}) (rate({m['requests']}{{{sel}}}[{rw}]))"

        def quantile(q: float) -> str:
            hist = f"{m['latency_histogram']}_bucket{{{sel}}}"
            return f"histogram_quantile({q}, sum by ({svc}, le) (rate({hist}[{rw}])))"

        if sli.key == "rps":
            return total, svc
        if sli.key == "error_rate":
            errors = f"{sel},{lb['status']}=~{label_value(self.error_status_regex)}"
            # "or ... * 0": a service with no 5xx at all has a ratio of 0, not no data.
            return (
                f"(sum by ({svc}) (rate({m['requests']}{{{errors}}}[{rw}])) or {total} * 0)"
                f" / {total}",
                svc,
            )
        if sli.key == "latency_p95":
            return quantile(0.95), svc
        if sli.key == "latency_p99":
            return quantile(0.99), svc
        if sli.key == "db_pool_utilization":
            return (
                f"sum by ({svc}) ({m['db_pool_active']}{{{sel}}})"
                f" / sum by ({svc}) ({m['db_pool_max']}{{{sel}}})",
                svc,
            )
        if sli.key == "db_pool_pending":
            return f"sum by ({svc}) ({m['db_pool_pending']}{{{sel}}})", svc
        if sli.key == "cache_up":
            return f"min by ({svc}) ({m['cache_up']}{{{sel}}})", svc
        if sli.key == "memory_rss":
            return f"max by ({svc}) ({m['memory_rss']}{{{sel}}})", svc
        if sli.key in ("restarts", "oom_killed"):
            if not scope.workloads:
                return None
            app = lb["k8s_app"]
            ns = {k: v for k, v in scope.filters.items() if k == lb["namespace"]}
            pod_sel = ",".join(f"{k}={label_value(v)}" for k, v in sorted(ns.items()))
            join = (
                f"* on ({lb['namespace']}, {lb['pod']}) group_left ({app}) "
                f"{m['pod_labels']}{{{app}=~{regex_alternation(scope.workloads)}}}"
            )
            if sli.key == "restarts":
                return f"max by ({app}) ({m['restarts']}{{{pod_sel}}} {join})", app
            return (
                f"max by ({app}) ({m['last_terminated_reason']}"
                f'{{{pod_sel}{"," if pod_sel else ""}reason="OOMKilled"}} {join})',
                app,
            )
        return None

    # -- the Metrics agent's questions --------------------------------------------------------

    def series_request(
        self, sli: SLI, scope: MetricScope, window: MetricWindow
    ) -> MetricRequest | None:
        built = self.promql(sli, scope)
        if built is None:
            return None
        query, group = built
        arguments = {
            "query": query,
            "start": iso_seconds(window.start),
            "end": iso_seconds(window.end),
            "step": f"{self.step_seconds(window)}s",
        }
        return MetricRequest(sli, ToolRequest(QUERY_RANGE, arguments), query, group)

    def series(self, request: MetricRequest, data: Any) -> dict[str, Values]:
        return parse_series(data, request.group)

    # -- links ----------------------------------------------------------------------------

    def ui_link(
        self, scope: MetricScope, request: MetricRequest, window: MetricWindow
    ) -> str | None:
        """Grafana panel (``settings.ui_link_template`` + ``settings.panels``), else the
        query link. ``{service} {namespace} {from_ms} {to_ms} {panel}`` are filled in."""
        panels = dict(self.settings.get("panels") or {})
        panel = panels.get(request.sli.panel) if request.sli.panel else None
        link = (
            format_link(
                self.settings.get("ui_link_template"),
                service=scope.service,
                namespace=scope.namespace,
                panel=panel,
                **window_values(window.start, window.end),
            )
            if panel is not None
            else None
        )
        return link or self.query_link(request, window)

    def query_link(self, request: MetricRequest, window: MetricWindow) -> str | None:
        """``settings.explore_link_template`` (``{query} {range} {end_utc}`` ...)."""
        return query_link(
            self.settings.get("explore_link_template"), request.query, window.start, window.end
        )


PROVIDER_REGISTRY.register(PrometheusMetrics)
