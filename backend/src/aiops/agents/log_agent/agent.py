"""Log (ELK) Agent: deterministic ES|QL overview first, then a bounded LLM investigation.

Vendor-neutral: index pattern comes from the service catalog, field names and the
UI link template come from the ``logs`` capability settings (config only).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from aiops.agents.base import AgentRun, AgentSpec, BaseAgent
from aiops.agents.registry import AGENTS
from aiops.core.models import AgentResult, Evidence, EvidenceKind
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
TOP_ERRORS = 10


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class LogScope:
    index: str
    fields: dict[str, str]
    error_levels: list[str]
    link_template: str | None
    service_value: str | None


class LogAgent(BaseAgent):
    spec = AgentSpec(
        name="logs",
        version="1",
        description="Investigates application logs (error patterns, counts, samples) via the logs capability.",
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
        return LogScope(
            index=str(index),
            fields=fields,
            error_levels=list(settings.get("error_levels", DEFAULT_ERROR_LEVELS)),
            link_template=settings.get("ui_link_template"),
            service_value=identifiers.get("service_value"),
        )

    def ui_link(self, scope: LogScope, run: AgentRun, kql: str) -> str | None:
        if not scope.link_template:
            return None
        window = run.task.context.time_range
        return scope.link_template.format(
            start=iso(window.start), end=iso(window.end), kql=quote(kql, safe="")
        )

    # -- deterministic overview -----------------------------------------------------------

    def _levels(self, scope: LogScope) -> str:
        return ", ".join(json.dumps(level) for level in scope.error_levels)

    async def overview(self, run: AgentRun, scope: LogScope) -> list[str]:
        """Level counts + top error messages. Returns lines for the LLM prompt."""
        f = scope.fields
        window = run.task.context.time_range
        bounds = {"start": iso(window.start), "end": iso(window.end)}
        lines: list[str] = []

        level_query = (
            f"FROM {scope.index} | STATS count = COUNT(*) BY {f['level']} | SORT count DESC"
        )
        outcome, evidence = await run.call_tool(
            "execute_esql",
            {"query": level_query, **bounds},
            summary=f"Log volume by level in {scope.index}",
        )
        if evidence is not None:
            rows = outcome.data.get("rows", []) if isinstance(outcome.data, dict) else []
            counts = {str(r[1]): int(r[0]) for r in rows if len(r) >= 2}
            total = sum(counts.values())
            errors = sum(counts.get(level, 0) for level in scope.error_levels)
            evidence.summary = (
                f"{total} log lines, {errors} at error level ({counts}) in {scope.index}"
            )
            evidence.link = self.ui_link(scope, run, "")
            lines.append(
                f"[{evidence.id}] Volume by level: {json.dumps(counts)} (total {total}, errors {errors})"
            )
        else:
            lines.append(f"Volume query failed: {outcome.tool_call.error}")

        top_query = (
            f"FROM {scope.index} | WHERE {f['level']} IN ({self._levels(scope)}) "
            f"| STATS count = COUNT(*), first_seen = MIN({f['timestamp']}), last_seen = MAX({f['timestamp']}) "
            f"BY msg = {f['message_keyword']} | SORT count DESC | LIMIT {TOP_ERRORS}"
        )
        outcome, evidence = await run.call_tool(
            "execute_esql",
            {"query": top_query, **bounds},
            summary=f"Top error messages in {scope.index}",
        )
        if evidence is not None:
            rows = outcome.data.get("rows", []) if isinstance(outcome.data, dict) else []
            kql = f"{f['level']}:({' or '.join(scope.error_levels)})"
            evidence.link = self.ui_link(scope, run, kql)
            if rows:
                evidence.summary = f"Top {len(rows)} error messages in {scope.index}"
                lines.append(
                    f"[{evidence.id}] Top error messages (count | first_seen | last_seen | message):"
                )
                lines.extend(f"  - {r[0]} | {r[1]} | {r[2]} | {r[3]}" for r in rows)
            else:
                evidence.summary = f"No error-level logs in {scope.index} for the window"
                lines.append(f"[{evidence.id}] No error-level log lines in the window.")
        else:
            lines.append(f"Top-errors query failed: {outcome.tool_call.error}")
        return lines

    # -- investigation -------------------------------------------------------------------

    def fields_table(self, scope: LogScope) -> str:
        return "\n".join(f"- {role}: `{name}`" for role, name in scope.fields.items())

    async def investigate(self, run: AgentRun, tool_specs: list[ToolSpec]) -> AgentResult:
        try:
            scope = self.scope(run)
        except LookupError as exc:
            return run.failed(str(exc))
        overview = await self.overview(run, scope)
        if not run.evidence:
            return run.failed("Could not query logs: " + " ".join(overview))

        system, user = self.build_prompt(run)
        user = f"{user}\n\n## Overview (computed for you)\n" + "\n".join(overview)
        result = await self.llm_loop(run, tool_specs, system, user)
        return self._attach_links(result, scope, run)

    def prompt_variables(self, run: AgentRun) -> dict[str, object]:
        variables = super().prompt_variables(run)
        scope = self.scope(run)
        variables.update(index=scope.index, fields_table=self.fields_table(scope))
        return variables

    def _attach_links(self, result: AgentResult, scope: LogScope, run: AgentRun) -> AgentResult:
        """Give LLM-driven evidence (samples, follow-up queries) a UI link too."""
        evidence: list[Evidence] = []
        for item in result.evidence:
            if item.link is None:
                item = item.model_copy(update={"link": self.ui_link(scope, run, "")})
            evidence.append(item)
        return result.model_copy(update={"evidence": evidence})


AGENTS.register(LogAgent)
