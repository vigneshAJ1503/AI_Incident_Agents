"""Log (ELK) Agent.

Investigation loop:
  1. deterministic ES|QL (4 calls): volume, message patterns and versions/startups
     for the incident window AND the previous 24h baseline, plus trace ids and
     first occurrence for the top anomalous pattern;
  2. deterministic analysis: templating, baseline diff, signals (no LLM math);
  3. bounded LLM follow-ups + an evidence-cited report;
  4. deterministic signals are merged into the report (they can't be dropped).

Vendor-neutral: index from the service catalog; field names, levels and the UI
link template from the ``logs`` capability settings. When all services share one
index (e.g. real Kubernetes logs, ``logs-k8s-*``), ``settings.service_filter: true``
narrows every query to ``fields.service == <catalog logs.service_value>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from aiops.agents.base import AgentRun, AgentSpec, BaseAgent
from aiops.agents.log_agent.analysis import DATA_SIGNALS, SIGNALS, LogAnalysis, analyze
from aiops.agents.log_agent.patterns import like_prefix
from aiops.agents.registry import AGENTS
from aiops.core.models import AgentResult, AgentStatus, Evidence, EvidenceKind
from aiops.llm.base import ToolSpec

DEFAULT_FIELDS = {
    "timestamp": "@timestamp",
    "level": "level",
    "message": "message",
    "message_keyword": "message.keyword",
    "service": "service",
    "trace_id": "trace_id",
}
DEFAULT_ERROR_LEVELS = ["ERROR", "FATAL", "CRITICAL"]
DEFAULT_PATTERN_LEVELS = ["ERROR", "FATAL", "CRITICAL", "WARN"]
DEFAULT_BASELINE_HOURS = 24.0
DEFAULT_STARTUP_PATTERN = "Starting*"
PATTERN_GROUP_LIMIT = 1000
TRACE_SAMPLE = 5


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def esql_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def esql_like(prefix: str) -> str:
    """LIKE pattern matching ``prefix`` literally, followed by anything."""
    pattern = prefix.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?") + "*"
    return esql_string(pattern)


@dataclass(frozen=True)
class LogScope:
    index: str
    fields: dict[str, str]
    error_levels: list[str]
    pattern_levels: list[str]
    baseline: timedelta
    startup_pattern: str
    link_template: str | None
    #: Service value to filter on when several services share one index (None = no filter).
    service_value: str | None = None

    @property
    def service_where(self) -> str | None:
        """ES|QL condition selecting this service's lines in a shared index."""
        if self.service_value is None:
            return None
        return f"{self.fields['service']} == {esql_string(self.service_value)}"

    @property
    def source(self) -> str:
        """``FROM <index>``, narrowed to the service when the index is shared."""
        where = self.service_where
        return f"FROM {self.index}" + (f" | WHERE {where}" if where else "")

    @property
    def service_kql(self) -> str:
        """KQL clause for UI links ('' when the index isn't shared)."""
        if self.service_value is None:
            return ""
        return f'{self.fields["service"]}:"{self.service_value}"'


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

    def scope(self, run: AgentRun) -> LogScope:
        ctx = run.task.context
        settings = self.deps.settings.capability("logs").settings
        fields = {**DEFAULT_FIELDS, **settings.get("fields", {})}
        fields.setdefault("message_keyword", f"{fields['message']}.keyword")
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

    def ui_link(self, scope: LogScope, run: AgentRun, kql: str) -> str | None:
        if not scope.link_template:
            return None
        window = run.task.context.time_range
        if scope.service_kql:
            kql = f"{scope.service_kql} and ({kql})" if kql else scope.service_kql
        return scope.link_template.format(
            start=iso(window.start), end=iso(window.end), kql=quote(kql, safe="")
        )

    # -- deterministic phase ---------------------------------------------------------------

    def _window_eval(self, scope: LogScope, run: AgentRun) -> str:
        start = iso(run.task.context.time_range.start)
        ts = scope.fields["timestamp"]
        return f'EVAL window = CASE({ts} >= TO_DATETIME("{start}"), "current", "baseline")'

    async def _esql(
        self, run: AgentRun, query: str, summary: str
    ) -> tuple[Any, Evidence | None, str | None]:
        window = run.task.context.time_range
        scope_start = window.start - self.scope(run).baseline
        outcome, evidence = await run.call_tool(
            "execute_esql",
            {"query": query, "start": iso(scope_start), "end": iso(window.end)},
            summary=summary,
        )
        return outcome.data, evidence, outcome.tool_call.error

    async def deterministic(
        self, run: AgentRun, scope: LogScope
    ) -> tuple[LogAnalysis | None, list[str]]:
        f = scope.fields
        levels = ", ".join(esql_string(level) for level in scope.pattern_levels)
        window_eval = self._window_eval(scope, run)
        notes: list[str] = []

        volume, volume_ev, err = await self._esql(
            run,
            f"{scope.source} | {window_eval} | STATS count = COUNT(*) BY window, level = {f['level']}",
            f"Log volume by level: incident window vs {scope.baseline} baseline",
        )
        if volume_ev is None:
            return None, [f"Volume query failed: {err}"]

        patterns, patterns_ev, err = await self._esql(
            run,
            f"{scope.source} | WHERE {f['level']} IN ({levels}) | {window_eval} "
            f"| STATS count = COUNT(*), first_seen = MIN({f['timestamp']}), last_seen = MAX({f['timestamp']}) "
            f"BY window, level = {f['level']}, msg = {f['message_keyword']} "
            f"| SORT count DESC | LIMIT {PATTERN_GROUP_LIMIT}",
            "Error/warning message patterns: incident window vs baseline",
        )
        if patterns_ev is None:
            notes.append(f"Pattern query failed: {err}")

        lifecycle: Any = None
        lifecycle_ev: Evidence | None = None
        if "version" in f:
            lifecycle, lifecycle_ev, err = await self._esql(
                run,
                f"{scope.source} | {window_eval} "
                f"| EVAL is_start = CASE({f['message']} LIKE {esql_string(scope.startup_pattern)}, 1, 0) "
                f"| STATS count = COUNT(*), starts = SUM(is_start), first_seen = MIN({f['timestamp']}) "
                f"BY window, version = {f['version']}",
                "Versions and startups: incident window vs baseline",
            )
            if lifecycle_ev is None:
                notes.append(f"Version query failed: {err}")

        window = run.task.context.time_range
        analysis = analyze(
            volume,
            patterns,
            lifecycle,
            error_levels=scope.error_levels,
            current=window.duration,
            baseline=scope.baseline,
        )

        # Evidence summaries + links now that numbers are known.
        volume_ev.summary = (
            f"{analysis.current_errors} errors in {analysis.current_total} lines during the incident window "
            f"vs {analysis.baseline_errors} in {analysis.baseline_total} over the previous {scope.baseline}"
        )
        volume_ev.link = self.ui_link(scope, run, "")
        notes.append(f"[{volume_ev.id}] volume")
        if patterns_ev is not None:
            new = [d for d in analysis.anomalous if d.is_new]
            patterns_ev.summary = (
                f"{len(analysis.deltas)} error/warn patterns in window; {len(analysis.anomalous)} anomalous "
                f"({len(new)} new vs baseline)"
            )
            patterns_ev.link = self.ui_link(
                scope, run, f"{f['level']}:({' or '.join(scope.pattern_levels)})"
            )
            notes.append(f"[{patterns_ev.id}] patterns")
        if lifecycle_ev is not None:
            deploys = (
                ", ".join(f"{d['version']} at {d['first_seen']}" for d in analysis.deployments)
                or "none"
            )
            lifecycle_ev.summary = (
                f"New versions in window: {deploys}; restarts: {analysis.restarts}"
            )
            lifecycle_ev.link = self.ui_link(scope, run, "")
            notes.append(f"[{lifecycle_ev.id}] versions")

        await self._trace_sample(run, scope, analysis, notes)
        return analysis, notes

    async def _trace_sample(
        self, run: AgentRun, scope: LogScope, analysis: LogAnalysis, notes: list[str]
    ) -> None:
        """Trace ids + first occurrence of the top anomalous pattern."""
        if not analysis.anomalous:
            return
        top = analysis.anomalous[0]
        prefix = like_prefix(top.pattern.template)
        if prefix is None:
            return
        f = scope.fields
        start = iso(run.task.context.time_range.start)
        keep = ", ".join(
            dict.fromkeys(f[k] for k in ("timestamp", "trace_id", "version") if k in f)
        )
        data, evidence, _ = await self._esql(
            run,
            f'{scope.source} | WHERE {f["timestamp"]} >= TO_DATETIME("{start}") '
            f"AND {f['message']} LIKE {esql_like(prefix)} "
            f"| KEEP {keep} | SORT {f['timestamp']} ASC | LIMIT {TRACE_SAMPLE}",
            f"First occurrences + trace ids of '{prefix}...'",
        )
        if evidence is None or not isinstance(data, dict):
            return
        columns, rows = data.get("columns", []), data.get("rows", [])
        trace_ids = [r[columns.index(f["trace_id"])] for r in rows if f["trace_id"] in columns]
        first = (
            rows[0][columns.index(f["timestamp"])] if rows and f["timestamp"] in columns else None
        )
        evidence.summary = f"First occurrence of '{prefix}...' at {first}; trace ids {trace_ids}"
        evidence.link = self.ui_link(scope, run, f'{f["message"]}:"{prefix}"')
        notes.append(
            f"[{evidence.id}] first occurrence {first}, trace ids: {', '.join(map(str, trace_ids))}"
        )

    # -- investigation -------------------------------------------------------------------

    def fields_table(self, scope: LogScope) -> str:
        table = "\n".join(f"- {role}: `{name}`" for role, name in scope.fields.items())
        if scope.service_where:
            table += (
                "\n\nThe index is shared by all services: every query MUST filter on this "
                f"service, e.g. `{scope.source} | ...` (search_logs: add `{scope.service_kql}` "
                "to the query)."
            )
        return table

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
        evidence = [
            e if e.link else e.model_copy(update={"link": self.ui_link(scope, run, "")})
            for e in result.evidence
        ]
        return result.model_copy(
            update={"signals": signals, "status": status, "evidence": evidence}
        )


AGENTS.register(LogAgent)
