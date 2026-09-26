"""Metrics Agent (UC-05).

Investigation loop:
  1. deterministic: one ``query_range`` per metric from the PromQL library (error ratio,
     p95/p99 latency, throughput, DB pool utilisation and waiters, cache, process memory,
     container restarts and OOM kills), each covering the service AND its catalog
     dependencies, over [window start - baseline, window end];
  2. deterministic analysis in Python: baseline median vs the window, sustained change
     point (start time), magnitude (ratio, z-score), signals;
  3. bounded LLM follow-ups + an evidence-cited report;
  4. finalize: signals and status come from the data (an LLM can neither invent nor hide
     an anomaly), "no metric anomaly" is said explicitly, metrics are never a root cause.

Vendor-neutral: label values from the service catalog (``metrics.labels``); metric names,
label names, windows and link templates from the ``metrics`` capability settings.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from aiops.agents.base import AgentRun, AgentSpec, BaseAgent
from aiops.agents.metrics_agent.analysis import (
    NO_ANOMALY_PHRASE,
    SIGNALS,
    MetricsAnalysis,
    SeriesStats,
    detect,
    downsample,
    fmt,
    iso,
    oom_stats,
    parse_series,
    restarts_stats,
)
from aiops.agents.metrics_agent.promql import MetricQuery, PromQLLibrary
from aiops.agents.registry import AGENTS
from aiops.core.config import ConfigError
from aiops.core.links import format_link, query_link, window_values
from aiops.core.models import (
    AgentResult,
    AgentStatus,
    ClaimKind,
    Evidence,
    EvidenceKind,
    TimeRange,
)
from aiops.llm.base import ToolSpec

DEFAULT_STEP_S = 30
DEFAULT_BASELINE_MINUTES = 30.0
#: Stay well inside prometheus-mcp's points-per-series cap.
MAX_POINTS = 1000


def parse_step(value: Any) -> int:
    text = str(value or "").strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    if text and text[-1] in units and text[:-1].isdigit():
        return int(text[:-1]) * units[text[-1]]
    return int(text) if text.isdigit() else DEFAULT_STEP_S


@dataclass(frozen=True)
class MetricsScope:
    service: str
    #: catalog service name -> value of the service label in the metrics
    label_values: dict[str, str]
    #: catalog service name -> value of the kube-state-metrics app label
    k8s_apps: dict[str, str]
    extra: dict[str, str]  # other label filters shared by all queries, e.g. namespace
    namespace: str
    dependencies: list[str]
    library: PromQLLibrary
    step_s: int
    baseline: timedelta
    link_template: str | None
    explore_template: str | None
    panels: dict[str, Any]


class MetricsAgent(BaseAgent):
    spec = AgentSpec(
        name="metrics",
        version="1",
        description="Analyzes service metrics (errors, latency, traffic, saturation, memory) vs a baseline and finds when they changed.",
        capabilities=["metrics"],
        evidence_kind=EvidenceKind.METRIC,
        prompt="metrics",
    )

    # -- scope ---------------------------------------------------------------------------

    def scope(self, run: AgentRun) -> MetricsScope:
        ctx = run.task.context
        if not ctx.service:
            raise LookupError("No service given: the Metrics agent needs a catalog service.")
        settings = self.deps.settings.capability("metrics").settings
        library = PromQLLibrary.from_settings(settings)
        service_label = library.labels["service"]
        namespace_label = library.labels["namespace"]

        def identifiers(name: str) -> tuple[dict[str, str], str] | None:
            try:
                entry = self.deps.catalog.get(name)
            except ConfigError:  # postgres, redis: not instrumented services
                return None
            ids = entry.identifiers("metrics", ctx.environment)
            labels = {str(k): str(v) for k, v in (ids.get("labels") or {}).items()}
            if not labels:
                return None
            k8s = entry.identifiers("k8s", ctx.environment)
            app = ids.get("k8s_app") or k8s.get("deployment") or labels.get(service_label, name)
            return labels, str(app)

        own = identifiers(ctx.service)
        if own is None:
            raise LookupError(
                f"No metric labels for '{ctx.service}' in '{ctx.environment}'. Add "
                "metrics.labels to the service catalog."
            )
        labels, app = own
        deps: dict[str, tuple[dict[str, str], str]] = {}
        for dep in self.deps.catalog.get(ctx.service).depends_on:
            found = identifiers(dep)
            if found is not None:
                deps[dep] = found
        extra = {k: v for k, v in labels.items() if k != service_label}
        return MetricsScope(
            service=ctx.service,
            label_values={
                ctx.service: labels.get(service_label, ctx.service),
                **{d: ids.get(service_label, d) for d, (ids, _) in deps.items()},
            },
            k8s_apps={ctx.service: app, **{d: a for d, (_, a) in deps.items()}},
            extra=extra,
            namespace=extra.get(namespace_label, ""),
            dependencies=list(deps),
            library=library,
            step_s=parse_step(settings.get("step", DEFAULT_STEP_S)),
            baseline=timedelta(
                minutes=float(settings.get("baseline_minutes", DEFAULT_BASELINE_MINUTES))
            ),
            link_template=settings.get("ui_link_template"),
            explore_template=settings.get("explore_link_template"),
            panels=dict(settings.get("panels") or {}),
        )

    # -- deterministic phase ---------------------------------------------------------------

    def _step(self, scope: MetricsScope, span: timedelta) -> int:
        return max(scope.step_s, math.ceil(span.total_seconds() / MAX_POINTS))

    async def deterministic(
        self, run: AgentRun, scope: MetricsScope
    ) -> tuple[MetricsAnalysis, list[str]]:
        window = run.task.context.time_range
        full = TimeRange(start=window.start - scope.baseline, end=window.end)
        baseline = TimeRange(start=full.start, end=window.start)
        step = self._step(scope, full.duration)
        by_value = {v: name for name, v in scope.label_values.items()}
        by_app = {v: name for name, v in scope.k8s_apps.items()}
        queries = scope.library.queries(
            list(scope.label_values.values()), scope.extra, list(scope.k8s_apps.values())
        )
        analysis = MetricsAnalysis(
            service=scope.service,
            dependencies=scope.dependencies,
            window=window,
            baseline_window=baseline,
        )
        notes: list[str] = []
        for mq in queries:
            outcome, evidence = await run.call_tool(
                "query_range",
                {
                    "query": mq.query,
                    "start": iso(full.start),
                    "end": iso(full.end),
                    "step": f"{step}s",
                },
                summary=mq.title,
            )
            if evidence is None:
                analysis.missing.append(mq.key)
                notes.append(f"{mq.key} query failed: {outcome.tool_call.error}")
                continue
            mapping = by_app if mq.group_label == scope.library.labels["k8s_app"] else by_value
            series = {
                mapping[k]: v
                for k, v in parse_series(outcome.data, mq.group_label).items()
                if k in mapping
            }
            stats = [self._stats(mq, name, values, window) for name, values in series.items()]
            analysis.stats.extend(stats)
            self._describe(evidence, mq, stats, series, scope, window, full)
            notes.append(f"[{evidence.id}] {mq.key}")
        return analysis, notes

    @staticmethod
    def _stats(mq: MetricQuery, service: str, values: Any, window: TimeRange) -> SeriesStats:
        if mq.key == "restarts":
            return restarts_stats(service, values, window)
        if mq.key == "oom_killed":
            return oom_stats(service, values, window)
        return detect(mq.key, service, mq.unit, values, window)

    def _describe(
        self,
        evidence: Evidence,
        mq: MetricQuery,
        stats: list[SeriesStats],
        series: dict[str, Any],
        scope: MetricsScope,
        window: TimeRange,
        full: TimeRange,
    ) -> None:
        """Replace the raw tool result with {metric, baseline, current, window, start_time},
        per-service stats, chart series and links."""
        own = next((s for s in stats if s.service == scope.service), None)
        parts: list[str] = []
        for s in sorted(stats, key=lambda s: (s.service != scope.service, s.service)):
            if s.anomaly is not None:
                parts.append(f"{s.service} {s.anomaly.describe()}")
            elif s.note:
                parts.append(f"{s.service}: {s.note}")
            else:
                parts.append(f"{s.service} normal ({fmt(s.current, s.unit)})")
        evidence.summary = f"{mq.title}: " + ("; ".join(parts) if parts else "no data")
        anomaly = own.anomaly if own else None
        evidence.timestamp = anomaly.start_time if anomaly else None
        values = window_values(full.start, full.end)
        panel = scope.panels.get(mq.panel) if mq.panel else None
        evidence.link = (
            format_link(
                scope.link_template,
                service=scope.label_values[scope.service],
                namespace=scope.namespace,
                panel=panel,
                **values,
            )
            if panel is not None
            else None
        ) or query_link(scope.explore_template, mq.query, full.start, full.end)
        evidence.data = {
            "metric": mq.key,
            "unit": mq.unit,
            "service": scope.service,
            "baseline": own.baseline if own else None,
            "current": own.current if own else None,
            "start_time": iso(anomaly.start_time) if anomaly else None,
            "window": {"start": iso(window.start), "end": iso(window.end)},
            "baseline_window": {"start": iso(full.start), "end": iso(window.start)},
            "query": mq.query,
            "query_link": query_link(scope.explore_template, mq.query, full.start, full.end),
            "services": {s.service: s.as_dict() for s in stats},
            "series": {name: downsample(values) for name, values in series.items()},
        }

    # -- investigation -------------------------------------------------------------------

    def prompt_variables(self, run: AgentRun) -> dict[str, object]:
        variables = super().prompt_variables(run)
        scope = self.scope(run)
        window = run.task.context.time_range
        variables.update(
            start=iso(window.start),
            end=iso(window.end),
            labels=json.dumps(
                {"service": scope.label_values[scope.service], **scope.extra}, sort_keys=True
            ),
            dependencies=", ".join(scope.dependencies) or "none",
            baseline_minutes=f"{scope.baseline.total_seconds() / 60:g}",
            signals=", ".join(f"`{s}`" for s in SIGNALS),
        )
        return variables

    async def investigate(self, run: AgentRun, tool_specs: list[ToolSpec]) -> AgentResult:
        try:
            scope = self.scope(run)
        except (LookupError, ConfigError) as exc:
            return run.failed(str(exc))
        analysis, notes = await self.deterministic(run, scope)
        if not analysis.stats:
            return run.failed("Could not query metrics: " + " ".join(notes))

        system, user = self.build_prompt(run)
        overview = "\n".join([*analysis.lines(), "Evidence ids: " + "; ".join(notes)])
        user = f"{user}\n\n## Overview (computed for you, deterministic)\n{overview}"
        result = await self.llm_loop(run, tool_specs, system, user)
        return self.finalize(result, analysis)

    def finalize(self, result: AgentResult, analysis: MetricsAnalysis) -> AgentResult:
        """Data decides signals and status; metrics stay symptoms, never a root cause."""
        if result.status not in (AgentStatus.SUCCESS, AgentStatus.NO_SIGNAL):
            return result  # partial/failed: keep the reason, don't invent signals
        signals = analysis.signals
        anomalous = any(s != "no_anomaly" for s in signals)
        status = AgentStatus.SUCCESS if anomalous else AgentStatus.NO_SIGNAL
        summary = result.summary
        if not anomalous and NO_ANOMALY_PHRASE.casefold() not in summary.casefold():
            summary = f"{analysis.no_anomaly_sentence()} {summary}".strip()
        findings = [
            f.model_copy(update={"kind": ClaimKind.HYPOTHESIS})
            if f.kind is ClaimKind.FACT and "root_cause" in f.type
            else f
            for f in result.findings
        ]
        return result.model_copy(
            update={
                "signals": signals,
                "status": status,
                "summary": summary,
                "findings": findings,
            }
        )


AGENTS.register(MetricsAgent)
