"""The ``metrics`` capability: what the Metrics agent asks, and the neutral series it gets.

The agent asks one question per SLI in ``SLIS`` ("series of <sli> for these services over
[start, end]"); a provider (``prometheus`` today; Datadog, New Relic, ... later) turns it
into its query language and MCP tool call, and normalizes the result into
``{series key: [(epoch seconds, value | None)]}``. The series key is the service's label
value (``SLI.per_workload`` False) or its Kubernetes workload/app name (True); the agent
maps keys back to catalog services.

Values are in the SLI's ``unit`` (ratio 0..1, seconds, requests/s, bytes, bool 0/1,
count). A provider that can't answer an SLI returns ``None`` from ``series_request``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from aiops.providers.base import Provider, ToolRequest

__all__ = [
    "SLI",
    "SLIS",
    "MetricRequest",
    "MetricScope",
    "MetricWindow",
    "MetricsProvider",
    "Unit",
    "Values",
    "iso_seconds",
]

Unit = Literal["ratio", "seconds", "rps", "bytes", "bool", "count"]
#: One series: [(epoch seconds, value or None when missing/not finite)].
Values = list[tuple[float, float | None]]


def iso_seconds(ts: datetime) -> str:
    """``2026-09-25T10:00:00Z`` (UTC, whole seconds)."""
    return ts.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class SLI:
    """A vendor-neutral indicator the Metrics agent analyzes."""

    key: str  # error_rate, latency_p95, ... (the analysis rules are keyed by it)
    title: str
    unit: Unit
    #: Dashboard panel role (a key of ``settings.panels``) for UI links, if any.
    panel: str | None = None
    #: Pod-level (restarts, OOM kills): series keyed by workload/app, not service label.
    per_workload: bool = False


#: The questions, in the order they are asked (the evidence order follows it).
SLIS: tuple[SLI, ...] = (
    SLI("rps", "Requests per second", "rps", "rps"),
    SLI("error_rate", "5xx error ratio", "ratio", "error_rate"),
    SLI("latency_p95", "p95 latency", "seconds", "latency"),
    SLI("latency_p99", "p99 latency", "seconds", "latency"),
    SLI("db_pool_utilization", "DB pool utilisation (active / max)", "ratio", "db_pool"),
    SLI("db_pool_pending", "Requests waiting for a DB connection", "count", "db_pool"),
    SLI("cache_up", "Cache reachable (1 = up)", "bool", "cache"),
    SLI("memory_rss", "Process memory (RSS)", "bytes", "memory"),
    SLI("restarts", "Container restarts (highest count among the pods)", "count", None, True),
    SLI("oom_killed", "Last termination was OOMKilled (1 = yes)", "bool", None, True),
)


@dataclass(frozen=True)
class MetricScope:
    """Which series to fetch (values come from the service catalog)."""

    #: service label values: the service first, then its catalog dependencies
    services: list[str]
    #: Kubernetes workload/app names of the same services (pod-level SLIs)
    workloads: list[str]
    #: other label filters shared by every query, e.g. {"namespace": "prod"}
    filters: dict[str, str]
    #: label value of the primary service and its namespace (for UI links)
    service: str
    namespace: str


@dataclass(frozen=True)
class MetricWindow:
    """The span to fetch: the baseline before the incident window, plus the window."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class MetricRequest:
    """How a provider answers one SLI: the tool call plus the query text (evidence)."""

    sli: SLI
    request: ToolRequest
    #: The vendor query, shown in evidence and used for the query link.
    query: str
    #: Result label/tag whose value keys each series.
    group: str


class MetricsProvider(Provider):
    """Interface of a ``metrics`` provider (override every ``NotImplementedError``)."""

    capability: ClassVar[str] = "metrics"
    #: Label roles -> this vendor's default label/tag names (``settings.labels`` wins).
    default_labels: ClassVar[Mapping[str, str]] = {}

    def labels(self) -> dict[str, str]:
        return {**self.default_labels, **(self.settings.get("labels") or {})}

    def label(self, role: str) -> str:
        """Label/tag name of a role: ``service``, ``namespace``, ... Catalog
        ``metrics.labels`` use these names as keys."""
        return self.labels().get(role, role)

    # -- the questions the Metrics agent asks ------------------------------------------------

    def series_request(
        self, sli: SLI, scope: MetricScope, window: MetricWindow
    ) -> MetricRequest | None:
        """The call that returns one series per service (or workload) for ``sli``;
        None when this provider can't answer it."""
        raise NotImplementedError

    def series(self, request: MetricRequest, data: Any) -> dict[str, Values]:
        """Normalize a tool result into ``{series key: [(ts, value)]}``."""
        raise NotImplementedError

    # -- links -----------------------------------------------------------------------------

    def ui_link(
        self, scope: MetricScope, request: MetricRequest, window: MetricWindow
    ) -> str | None:
        """Deep link for a piece of evidence (dashboard panel, else the query)."""
        raise NotImplementedError

    def query_link(self, request: MetricRequest, window: MetricWindow) -> str | None:
        """Link that opens exactly ``request.query`` over the window (None = unsupported)."""
        return None
