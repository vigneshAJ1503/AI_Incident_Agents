"""SKELETON, NOT REGISTERED: a Datadog-shaped starting point for a metrics provider.

Files starting with ``_`` are never imported by ``aiops.providers``, so this one is
inert (``metrics/datadog`` stays *planned* in the provider matrix). Steps
(docs/portability.md, "How to add a provider"):

1. Copy this file to ``providers/metrics/datadog.py`` (for example) and rename the class.
2. Implement ``series_request`` for every SLI in ``aiops.providers.metrics.SLIS`` the
   vendor can answer (return None for the others): one call that returns one series per
   service (or per workload for ``per_workload`` SLIs), in the SLI's unit.
3. Implement ``series`` so results become ``{series key: [(epoch seconds, value)]}``,
   keyed by ``request.group`` (e.g. the ``service`` tag).
4. Uncomment ``PROVIDER_REGISTRY.register(...)`` and remove the static ``datadog`` row
   from ``core/profiles.py``.
5. Add the prompt fragment ``config/prompts/providers/metrics/<name>/v1.md``.
6. Name the provider in the profile (``capabilities.metrics.provider: datadog``) with
   its MCP server, ``tool_allowlist`` and settings; ``aiops profile validate`` checks
   ``required``.
7. Add contract tests: golden queries per SLI + normalization (tests/unit/test_providers.py).
"""

from __future__ import annotations

from typing import Any, ClassVar

from aiops.providers.base import ToolRequest
from aiops.providers.metrics import (
    SLI,
    MetricRequest,
    MetricScope,
    MetricsProvider,
    MetricWindow,
    Values,
)

#: Datadog metric queries per SLI ({sel} = tag filter). Adjust to the company's metrics.
QUERIES = {
    "rps": "sum:trace.http.request.hits{{{sel}}} by {{service}}.as_rate()",
    "error_rate": "sum:trace.http.request.errors{{{sel}}} by {{service}}.as_rate() / "
    "sum:trace.http.request.hits{{{sel}}} by {{service}}.as_rate()",
    "latency_p95": "p95:trace.http.request{{{sel}}} by {{service}}",
    "latency_p99": "p99:trace.http.request{{{sel}}} by {{service}}",
}


class SkeletonDatadogMetrics(MetricsProvider):
    name: ClassVar[str] = "datadog"
    mcp: ClassVar[str] = "datadog-mcp"
    required: ClassVar[tuple[str, ...]] = ("site",)
    agent_tools: ClassVar[tuple[str, ...]] = ("query_metrics",)
    note: ClassVar[str] = "example only"
    prompt_fragment: ClassVar[str | None] = "providers/metrics/datadog"
    default_labels: ClassVar[dict[str, str]] = {"service": "service", "namespace": "kube_namespace"}

    def series_request(
        self, sli: SLI, scope: MetricScope, window: MetricWindow
    ) -> MetricRequest | None:
        template = QUERIES.get(sli.key)
        if template is None:
            return None  # not answered by this provider: skipped, never an anomaly
        tags = [f"{self.label('service')}:{s}" for s in scope.services]
        tags += [f"{k}:{v}" for k, v in sorted(scope.filters.items())]
        query = template.format(sel=",".join(tags))
        request = ToolRequest(
            "query_metrics",
            {
                "query": query,
                "from": int(window.start.timestamp()),
                "to": int(window.end.timestamp()),
            },
        )
        return MetricRequest(sli, request, query, self.label("service"))

    def series(self, request: MetricRequest, data: Any) -> dict[str, Values]:
        raise NotImplementedError("map each series' scope tag to [(ts seconds, value)]")

    def ui_link(
        self, scope: MetricScope, request: MetricRequest, window: MetricWindow
    ) -> str | None:
        return None  # e.g. a Datadog notebook/dashboard link from settings.ui_link_template


# PROVIDER_REGISTRY.register(SkeletonDatadogMetrics)  # from aiops.providers.registry import PROVIDER_REGISTRY
