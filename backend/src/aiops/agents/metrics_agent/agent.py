"""Metrics Agent (UC-05).

Investigation loop:
  1. deterministic: one provider query per SLI (error ratio, p95/p99 latency, throughput,
     DB pool utilisation and waiters, cache, process memory, container restarts and OOM
     kills), each covering the service AND its catalog dependencies, over
     [window start - baseline, window end];
  2. deterministic analysis in Python: baseline median vs the window, sustained change
     point (start time), magnitude (ratio, z-score), signals; a series with no baseline
     (fresh cluster) is judged against ``settings.absolute_thresholds`` instead;
  3. bounded LLM follow-ups + an evidence-cited report;
  4. finalize: signals and status come from the data (an LLM can neither invent nor hide
     an anomaly), "no metric anomaly" is said explicitly, metrics are never a root cause.

Vendor-neutral: the agent asks the ``metrics`` provider adapter
(``capabilities.metrics.provider``, ADR-0012) for one series per SLI
(``aiops.providers.metrics.SLIS``) over services from the service catalog
(``metrics.labels``); the provider owns the query language (PromQL for ``prometheus``),
the tool calls and the deep links.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from aiops.agents.base import AgentRun, AgentSpec, BaseAgent
from aiops.agents.metrics_agent.analysis import (
    NO_ANOMALY_PHRASE,
    SIGNALS,
    MetricsAnalysis,
    SeriesStats,
    absolute_thresholds,
    detect,
    downsample,
    fmt,
    iso,
    oom_stats,
    restarts_stats,
)
from aiops.agents.registry import AGENTS
from aiops.core.config import ConfigError
from aiops.core.models import (
    AgentResult,
    AgentStatus,
    ClaimKind,
    Evidence,
    EvidenceKind,
    TimeRange,
)
from aiops.llm.base import ToolSpec
from aiops.providers.metrics import (
    SLIS,
    MetricRequest,
    MetricScope,
    MetricsProvider,
    MetricWindow,
    Values,
)

DEFAULT_BASELINE_MINUTES = 30.0


@dataclass(frozen=True)
class MetricsScope:
    service: str
    #: catalog service name -> value of the service label in the metrics
    label_values: dict[str, str]
    #: catalog service name -> Kubernetes workload/app name (pod-level SLIs)
    k8s_apps: dict[str, str]
    extra: dict[str, str]  # other label filters shared by all queries, e.g. namespace
    namespace: str
    dependencies: list[str]
    provider: MetricsProvider
    baseline: timedelta
    #: no-baseline fallback: metric -> absolute threshold (settings.absolute_thresholds)
    absolute: dict[str, float]

    @property
    def neutral(self) -> MetricScope:
        """What the provider needs: label values only, no catalog names."""
        return MetricScope(
            services=list(self.label_values.values()),
            workloads=list(self.k8s_apps.values()),
            filters=self.extra,
            service=self.label_values[self.service],
            namespace=self.namespace,
        )


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
        provider = self.provider("metrics", MetricsProvider)
        service_label = provider.label("service")
        namespace_label = provider.label("namespace")

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
        minutes = provider.settings.get("baseline_minutes", DEFAULT_BASELINE_MINUTES)
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
            provider=provider,
            baseline=timedelta(minutes=float(minutes)),
            absolute=absolute_thresholds(provider.settings.get("absolute_thresholds")),
        )

    # -- deterministic phase ---------------------------------------------------------------

    async def deterministic(
        self, run: AgentRun, scope: MetricsScope
    ) -> tuple[MetricsAnalysis, list[str]]:
        window = run.task.context.time_range
        span = MetricWindow(start=window.start - scope.baseline, end=window.end)
        baseline = TimeRange(start=span.start, end=window.start)
        neutral = scope.neutral
        by_value = {v: name for name, v in scope.label_values.items()}
        by_app = {v: name for name, v in scope.k8s_apps.items()}
        analysis = MetricsAnalysis(
            service=scope.service,
            dependencies=scope.dependencies,
            window=window,
            baseline_window=baseline,
        )
        notes: list[str] = []
        for sli in SLIS:
            mr = scope.provider.series_request(sli, neutral, span)
            if mr is None:  # this provider (or scope) can't answer the SLI
                continue
            outcome, evidence = await run.call_tool(
                mr.request.tool, mr.request.arguments, summary=sli.title
            )
            if evidence is None:
                analysis.missing.append(sli.key)
                notes.append(f"{sli.key} query failed: {outcome.tool_call.error}")
                continue
            mapping = by_app if sli.per_workload else by_value
            series = {
                mapping[k]: v
                for k, v in scope.provider.series(mr, outcome.data).items()
                if k in mapping
            }
            stats = [
                self._stats(mr, name, values, window, scope.absolute)
                for name, values in series.items()
            ]
            analysis.stats.extend(stats)
            self._describe(evidence, mr, stats, series, scope, window, span)
            notes.append(f"[{evidence.id}] {sli.key}")
        return analysis, notes

    @staticmethod
    def _stats(
        mr: MetricRequest,
        service: str,
        values: Values,
        window: TimeRange,
        absolute: dict[str, float],
    ) -> SeriesStats:
        if mr.sli.key == "restarts":
            return restarts_stats(service, values, window)
        if mr.sli.key == "oom_killed":
            return oom_stats(service, values, window)
        return detect(mr.sli.key, service, mr.sli.unit, values, window, absolute)

    def _describe(
        self,
        evidence: Evidence,
        mr: MetricRequest,
        stats: list[SeriesStats],
        series: dict[str, Values],
        scope: MetricsScope,
        window: TimeRange,
        span: MetricWindow,
    ) -> None:
        """Replace the raw tool result with {metric, baseline, current, window, start_time},
        per-service stats, chart series and links."""
        sli = mr.sli
        own = next((s for s in stats if s.service == scope.service), None)
        parts: list[str] = []
        for s in sorted(stats, key=lambda s: (s.service != scope.service, s.service)):
            if s.anomaly is not None:
                parts.append(f"{s.service} {s.anomaly.describe()}")
            elif s.note:
                parts.append(f"{s.service}: {s.note}")
            else:
                parts.append(f"{s.service} normal ({fmt(s.current, s.unit)})")
        evidence.summary = f"{sli.title}: " + ("; ".join(parts) if parts else "no data")
        anomaly = own.anomaly if own else None
        evidence.timestamp = anomaly.start_time if anomaly else None
        evidence.link = scope.provider.ui_link(scope.neutral, mr, span)
        evidence.data = {
            "metric": sli.key,
            "unit": sli.unit,
            "service": scope.service,
            "baseline": own.baseline if own else None,
            "current": own.current if own else None,
            "start_time": iso(anomaly.start_time) if anomaly else None,
            "window": {"start": iso(window.start), "end": iso(window.end)},
            "baseline_window": {"start": iso(span.start), "end": iso(window.start)},
            "query": mr.query,
            "query_link": scope.provider.query_link(mr, span),
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
