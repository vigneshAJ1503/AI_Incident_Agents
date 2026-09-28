"""Log Agent.

Investigation loop:
  1. deterministic queries (4 calls): volume, message patterns and versions/startups
     for the incident window AND the previous 24h baseline, plus trace ids and
     first occurrence for the top anomalous pattern;
  2. deterministic analysis: templating, baseline diff, signals (no LLM math);
  3. bounded LLM follow-ups + an evidence-cited report;
  4. deterministic signals are merged into the report (they can't be dropped).

Vendor-neutral: the ``logs`` provider adapter (``capabilities.logs.provider``, ADR-0012)
turns each question into the vendor's query language and tool calls, and normalizes the
results. Index from the service catalog; field names, levels and the UI link template
from the ``logs`` capability settings. When all services share one index (e.g. real
Kubernetes logs, ``logs-k8s-*``), ``settings.service_filter: true`` narrows every query
to ``fields.service == <catalog logs.service_value>``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from aiops.agents.base import AgentRun, AgentSpec, BaseAgent
from aiops.agents.log_agent.analysis import DATA_SIGNALS, SIGNALS, LogAnalysis, analyze
from aiops.agents.registry import AGENTS
from aiops.core.models import AgentResult, AgentStatus, Evidence, EvidenceKind, parse_timestamp
from aiops.llm.base import ToolSpec
from aiops.providers.base import ToolRequest
from aiops.providers.logs import LogScope, LogsProvider, LogTable, LogWindow, iso

DEFAULT_ERROR_LEVELS = ["ERROR", "FATAL", "CRITICAL"]
DEFAULT_PATTERN_LEVELS = ["ERROR", "FATAL", "CRITICAL", "WARN"]
DEFAULT_BASELINE_HOURS = 24.0
DEFAULT_STARTUP_PATTERN = "Starting*"
PATTERN_GROUP_LIMIT = 1000
TRACE_SAMPLE = 5

__all__ = ["LogAgent", "LogScope"]


class LogAgent(BaseAgent):
    spec = AgentSpec(
        name="logs",
        version="2",
        description="Investigates application logs: error patterns vs a 24h baseline, deployments, restarts, trace ids.",
        capabilities=["logs"],
        evidence_kind=EvidenceKind.LOG,
        prompt="logs",
    )

    # -- scope ---------------------------------------------------------------------------

    def logs(self) -> LogsProvider:
        return self.provider("logs", LogsProvider)

    def scope(self, run: AgentRun) -> LogScope:
        ctx = run.task.context
        settings = self.deps.settings.capability("logs").settings
        fields = self.logs().fields()
        identifiers: dict[str, Any] = {}
        if ctx.service:
            identifiers = self.deps.catalog.get(ctx.service).identifiers("logs", ctx.environment)
        index = identifiers.get("index_pattern") or settings.get("index_pattern")
        if not index:
            raise LookupError(
                f"No log index configured for service '{ctx.service}' in '{ctx.environment}'. "
                "Add logs.index_pattern to the service catalog."
            )
        # Shared index (e.g. every pod's logs in logs-k8s-*): opt-in filter on the service
        # field, value from the catalog (logs.service_value, default: the service name).
        service_value: str | None = None
        if settings.get("service_filter") and ctx.service:
            service_value = str(identifiers.get("service_value") or ctx.service)
        return LogScope(
            index=str(index),
            fields=fields,
            error_levels=list(settings.get("error_levels", DEFAULT_ERROR_LEVELS)),
            pattern_levels=list(settings.get("pattern_levels", DEFAULT_PATTERN_LEVELS)),
            baseline=timedelta(hours=float(settings.get("baseline_hours", DEFAULT_BASELINE_HOURS))),
            startup_pattern=str(settings.get("startup_pattern", DEFAULT_STARTUP_PATTERN)),
            link_template=settings.get("ui_link_template"),
            service_value=service_value,
        )

    def window(self, scope: LogScope, run: AgentRun) -> LogWindow:
        time_range = run.task.context.time_range
        return LogWindow(start=time_range.start, end=time_range.end, baseline=scope.baseline)

    # -- deterministic phase ---------------------------------------------------------------

    async def _query(
        self, run: AgentRun, request: ToolRequest, summary: str
    ) -> tuple[LogTable | None, Evidence | None, str | None]:
        outcome, evidence = await run.call_tool(request.tool, request.arguments, summary=summary)
        return self.logs().table(request, outcome.data), evidence, outcome.tool_call.error

    async def deterministic(
        self, run: AgentRun, scope: LogScope
    ) -> tuple[LogAnalysis | None, list[str]]:
        logs = self.logs()
        window = self.window(scope, run)
        notes: list[str] = []

        volume, volume_ev, err = await self._query(
            run,
            logs.volume_by_level(scope, window),
            f"Log volume by level: incident window vs {scope.baseline} baseline",
        )
        if volume_ev is None:
            return None, [f"Volume query failed: {err}"]

        patterns, patterns_ev, err = await self._query(
            run,
            logs.message_patterns(scope, window, PATTERN_GROUP_LIMIT),
            "Error/warning message patterns: incident window vs baseline",
        )
        if patterns_ev is None:
            notes.append(f"Pattern query failed: {err}")

        lifecycle: LogTable | None = None
        lifecycle_ev: Evidence | None = None
        if "version" in scope.fields:
            lifecycle, lifecycle_ev, err = await self._query(
                run,
                logs.versions_and_startups(scope, window),
                "Versions and startups: incident window vs baseline",
            )
            if lifecycle_ev is None:
                notes.append(f"Version query failed: {err}")

        analysis = analyze(
            volume,
            patterns,
            lifecycle,
            error_levels=scope.error_levels,
            current=run.task.context.time_range.duration,
            baseline=scope.baseline,
        )

        # Evidence summaries + links now that numbers are known.
        volume_ev.summary = (
            f"{analysis.current_errors} errors in {analysis.current_total} lines during the incident window "
            f"vs {analysis.baseline_errors} in {analysis.baseline_total} over the previous {scope.baseline}"
        )
        volume_ev.link = logs.ui_link(scope, window)
        notes.append(f"[{volume_ev.id}] volume")
        if patterns_ev is not None:
            new = [d for d in analysis.anomalous if d.is_new]
            patterns_ev.summary = (
                f"{len(analysis.deltas)} error/warn patterns in window; {len(analysis.anomalous)} anomalous "
                f"({len(new)} new vs baseline)"
            )
            patterns_ev.link = logs.ui_link(scope, window, levels=scope.pattern_levels)
            # Structured for the orchestrator: the knowledge agent searches these templates.
            patterns_ev.data["patterns"] = [d.pattern.template for d in analysis.anomalous]
            patterns_ev.timestamp = _earliest(d.pattern.first_seen for d in analysis.anomalous)
            notes.append(f"[{patterns_ev.id}] patterns")
        if lifecycle_ev is not None:
            deploys = (
                ", ".join(f"{d['version']} at {d['first_seen']}" for d in analysis.deployments)
                or "none"
            )
            lifecycle_ev.summary = (
                f"New versions in window: {deploys}; restarts: {analysis.restarts}"
            )
            lifecycle_ev.link = logs.ui_link(scope, window)
            lifecycle_ev.timestamp = _earliest(d.get("first_seen") for d in analysis.deployments)
            notes.append(f"[{lifecycle_ev.id}] versions")

        await self._trace_sample(run, scope, analysis, notes)
        return analysis, notes

    async def _trace_sample(
        self, run: AgentRun, scope: LogScope, analysis: LogAnalysis, notes: list[str]
    ) -> None:
        """Trace ids + first occurrence of the top anomalous pattern."""
        if not analysis.anomalous:
            return
        logs = self.logs()
        prefix = logs.phrase(analysis.anomalous[0].pattern.template)
        if prefix is None:
            return
        window = self.window(scope, run)
        table, evidence, _ = await self._query(
            run,
            logs.first_occurrences(scope, window, prefix, TRACE_SAMPLE),
            f"First occurrences + trace ids of '{prefix}...'",
        )
        if evidence is None or table is None:
            return
        columns, rows = table.columns, table.rows
        trace_ids = [r[columns.index("trace_id")] for r in rows if "trace_id" in columns]
        first = rows[0][columns.index("timestamp")] if rows and "timestamp" in columns else None
        evidence.summary = f"First occurrence of '{prefix}...' at {first}; trace ids {trace_ids}"
        evidence.timestamp = parse_timestamp(first)
        evidence.link = logs.ui_link(scope, window, phrase=prefix)
        notes.append(
            f"[{evidence.id}] first occurrence {first}, trace ids: {', '.join(map(str, trace_ids))}"
        )

    # -- investigation -------------------------------------------------------------------

    def fields_table(self, scope: LogScope) -> str:
        table = "\n".join(f"- {role}: `{name}`" for role, name in scope.fields.items())
        return table + self.logs().scope_note(scope)

    def prompt_variables(self, run: AgentRun) -> dict[str, object]:
        variables = super().prompt_variables(run)
        scope = self.scope(run)
        window = run.task.context.time_range
        variables.update(
            index=scope.index,
            fields_table=self.fields_table(scope),
            start=iso(window.start),
            end=iso(window.end),
            baseline_start=iso(window.start - scope.baseline),
            signals=", ".join(f"`{s}`" for s in SIGNALS),
        )
        return variables

    async def investigate(self, run: AgentRun, tool_specs: list[ToolSpec]) -> AgentResult:
        try:
            scope = self.scope(run)
        except LookupError as exc:
            return run.failed(str(exc))
        analysis, notes = await self.deterministic(run, scope)
        if analysis is None:
            return run.failed("Could not query logs: " + " ".join(notes))

        system, user = self.build_prompt(run)
        overview = "\n".join([*analysis.lines(), "Evidence ids: " + "; ".join(notes)])
        user = f"{user}\n\n## Overview (computed for you, deterministic)\n{overview}"
        result = await self.llm_loop(run, tool_specs, system, user)
        return self.finalize(result, analysis, scope, run)

    def finalize(
        self, result: AgentResult, analysis: LogAnalysis, scope: LogScope, run: AgentRun
    ) -> AgentResult:
        """Merge deterministic signals; anomalies can't be reported as 'no_signal'."""
        # Data signals are authoritative; the LLM may only add semantic ones, and only
        # when the data shows something anomalous.
        llm_extra = {s for s in result.signals if s in SIGNALS and s not in DATA_SIGNALS}
        if not analysis.anomalous:
            llm_extra = set()
        signals = [s for s in SIGNALS if s in {*analysis.signals, *llm_extra}]
        status = result.status
        if status is AgentStatus.NO_SIGNAL and analysis.anomalous:
            status = AgentStatus.SUCCESS
        link = self.logs().ui_link(scope, self.window(scope, run))
        evidence = [e if e.link else e.model_copy(update={"link": link}) for e in result.evidence]
        return result.model_copy(
            update={"signals": signals, "status": status, "evidence": evidence}
        )


def _earliest(values: Iterable[Any]) -> datetime | None:
    """Earliest parseable timestamp (evidence timestamps feed the investigation timeline)."""
    parsed = [ts for ts in (parse_timestamp(v) for v in values) if ts is not None]
    return min(parsed) if parsed else None


AGENTS.register(LogAgent)
