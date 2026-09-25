"""Tickets (Jira) Agent: related tickets and known issues for an incident (UC-03).

Investigation:
  1. deterministic JQL (1-2 searches): the service's (and its dependencies') components
     and labels from the service catalog, plus symptom terms from the question, symptoms
     and hints; open issues and issues resolved within the look-back window;
  2. deterministic relevance: same service / dependency / other x symptom match ->
     known issue, similar past incident, context; signals decided here;
  3. bounded LLM follow-ups (e.g. jira_get_issue for comments) + an evidence-cited report;
  4. deterministic signals and status are authoritative (the LLM can't claim a known issue).

Vendor-neutral: tool names/shapes are the ``tickets`` capability contract (aiops.mcp.tickets);
project key, link template and look-back come from ``capabilities.tickets.settings``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from aiops.agents.base import AgentRun, AgentSpec, BaseAgent
from aiops.agents.registry import AGENTS
from aiops.agents.tickets_agent.analysis import (
    SIGNALS,
    Assessment,
    ServiceScope,
    TicketAnalysis,
    assess,
    keyword_jql,
    load_terms,
    scope_jql,
    symptom_terms,
)
from aiops.core.config import ConfigError
from aiops.core.models import AgentResult, AgentStatus, Evidence, EvidenceKind
from aiops.llm.base import ToolSpec
from aiops.mcp import tickets
from aiops.mcp.tickets import Ticket

DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_PROJECT = "OPS"
#: Tickets that become individual evidence items (most relevant first).
MAX_TICKET_EVIDENCE = 8


class TicketsAgent(BaseAgent):
    spec = AgentSpec(
        name="tickets",
        version="1",
        description="Finds related tickets: open known issues, similar past incidents, dependency issues.",
        capabilities=["tickets"],
        evidence_kind=EvidenceKind.TICKET,
        prompt="tickets",
    )

    # -- scope ---------------------------------------------------------------------------

    def _settings(self) -> dict[str, Any]:
        return self.deps.settings.capability("tickets").settings

    def _service_scope(self, name: str, environment: str | None) -> ServiceScope | None:
        ids = self.deps.catalog.get(name).identifiers("tickets", environment)
        components = tuple(str(c) for c in ids.get("components", []))
        labels = tuple(str(label) for label in ids.get("labels", []))
        if not components and not labels:
            return None
        return ServiceScope(name, components, labels)

    def analysis_scope(self, run: AgentRun) -> TicketAnalysis:
        ctx = run.task.context
        settings = self._settings()
        service: ServiceScope | None = None
        dependencies: list[ServiceScope] = []
        if ctx.service:
            service = self._service_scope(ctx.service, ctx.environment)
            for dep in self.deps.catalog.get(ctx.service).depends_on:
                try:
                    scope = self._service_scope(dep, ctx.environment)
                except ConfigError:  # e.g. postgres: infrastructure, not a catalog service
                    continue
                if scope:
                    dependencies.append(scope)
        terms = symptom_terms(
            ctx.question, ctx.symptoms, run.task.hints, load_terms(settings.get("symptom_terms"))
        )
        lookback = timedelta(
            days=float(settings.get("resolved_lookback_days", DEFAULT_LOOKBACK_DAYS))
        )
        return TicketAnalysis(
            project=str(settings.get("project_key", DEFAULT_PROJECT)),
            service=service,
            dependencies=dependencies,
            terms=terms,
            cutoff=ctx.time_range.end - lookback,
        )

    def ui_link(self, key: str) -> str | None:
        template = self._settings().get("ui_link_template")
        return str(template).format(key=key) if template else None

    # -- deterministic phase ---------------------------------------------------------------

    async def _search(
        self, run: AgentRun, jql: str, summary: str
    ) -> tuple[list[Ticket], Evidence | None, str | None]:
        limit = self.deps.settings.capability("tickets").limits.max_results
        outcome, evidence = await run.call_tool(
            tickets.SEARCH,
            {"jql": jql, "fields": tickets.TICKET_FIELDS, "limit": limit},
            summary=summary,
        )
        if evidence is None:
            return [], None, outcome.tool_call.error
        return tickets.parse_search(outcome.data, outcome.text), evidence, None

    async def deterministic(self, run: AgentRun, analysis: TicketAnalysis) -> list[str]:
        """Run the searches and assess every ticket. Returns evidence notes for the LLM."""
        found: dict[str, Ticket] = {}
        notes: list[str] = []
        scopes = [s for s in [analysis.service, *analysis.dependencies] if s]
        if scopes:
            components = list(dict.fromkeys(c for s in scopes for c in s.components))
            labels = list(dict.fromkeys(label for s in scopes for label in s.labels))
            jql = scope_jql(analysis.project, components, labels, analysis.cutoff)
            hits, evidence, error = await self._search(
                run, jql, "Tickets for the service and its dependencies"
            )
            if evidence is None and components:
                # Real Jira rejects unknown component names; retry with labels only.
                analysis.notes.append(
                    f"Component search failed ({error}); retried with labels only."
                )
                jql = scope_jql(analysis.project, [], labels, analysis.cutoff)
                hits, evidence, error = await self._search(
                    run, jql, "Tickets for the service (labels)"
                )
            if evidence is None:
                analysis.notes.append(f"Service ticket search failed: {error}")
            else:
                evidence.summary = f"{len(hits)} open or recently resolved tickets for {', '.join(s.name for s in scopes)}"
                notes.append(f"[{evidence.id}] scope_search")
                found.update((t.key, t) for t in hits)
        if analysis.terms:
            jql = keyword_jql(analysis.project, analysis.terms, analysis.cutoff)
            hits, evidence, error = await self._search(run, jql, "Tickets mentioning the symptoms")
            if evidence is None:
                analysis.notes.append(f"Symptom ticket search failed: {error}")
            else:
                terms = ", ".join(t.name for t in analysis.terms)
                evidence.summary = (
                    f"{len(hits)} open or recently resolved tickets mentioning {terms}"
                )
                notes.append(f"[{evidence.id}] keyword_search")
                found.update((t.key, t) for t in hits)

        analysis.assessments = [
            assess(t, analysis.service, analysis.dependencies, analysis.terms)
            for t in found.values()
        ]
        for assessment in analysis.relevant[:MAX_TICKET_EVIDENCE]:
            assessment.evidence_id = self._ticket_evidence(run, assessment).id
        return notes

    def _ticket_evidence(self, run: AgentRun, a: Assessment) -> Evidence:
        t = a.ticket
        state = (
            "open" if t.is_open else f"resolved {t.resolved:%Y-%m-%d}" if t.resolved else "closed"
        )
        return run.add_evidence(
            Evidence(
                kind=EvidenceKind.TICKET,
                source=f"tickets.{tickets.SEARCH}",
                summary=f"{t.key} [{t.status}, {state}] {t.summary}",
                link=self.ui_link(t.key) or t.url,
                timestamp=t.created,
                data={
                    "key": t.key,
                    "status": t.status,
                    "status_category": t.status_category,
                    "issue_type": t.issue_type,
                    "priority": t.priority,
                    "labels": t.labels,
                    "components": t.components,
                    "resolution": t.resolution,
                    "resolved": t.resolved.isoformat() if t.resolved else None,
                    "relation": a.relation,
                    "related_service": a.related_service,
                    "matched_symptoms": a.matched,
                    "category": a.category,
                    "description": t.description[:500],
                },
            )
        )

    # -- investigation -------------------------------------------------------------------

    def prompt_variables(self, run: AgentRun) -> dict[str, object]:
        variables = super().prompt_variables(run)
        analysis = self.analysis_scope(run)
        variables.update(
            project_key=analysis.project,
            lookback_start=f"{analysis.cutoff:%Y-%m-%d}",
            signals=", ".join(f"`{s}`" for s in SIGNALS),
        )
        return variables

    async def investigate(self, run: AgentRun, tool_specs: list[ToolSpec]) -> AgentResult:
        analysis = self.analysis_scope(run)
        if analysis.service is None and not analysis.terms:
            return run.failed(
                "Nothing to search tickets by: no catalog ticket identifiers for the service "
                "and no symptom terms in the question."
            )
        notes = await self.deterministic(run, analysis)
        if not notes:
            return run.failed("Could not search tickets: " + " ".join(analysis.notes))

        system, user = self.build_prompt(run)
        overview = "\n".join([*analysis.lines(), "Evidence ids: " + "; ".join(notes)])
        user = f"{user}\n\n## Overview (computed for you, deterministic)\n{overview}"
        result = await self.llm_loop(run, tool_specs, system, user)
        return self.finalize(result, analysis)

    def finalize(self, result: AgentResult, analysis: TicketAnalysis) -> AgentResult:
        """Signals and status come from the data; the LLM can't claim or hide a known issue."""
        status = result.status
        if status in (AgentStatus.SUCCESS, AgentStatus.NO_SIGNAL):
            status = AgentStatus.SUCCESS if analysis.strong else AgentStatus.NO_SIGNAL
        return result.model_copy(update={"signals": analysis.signals, "status": status})


AGENTS.register(TicketsAgent)
