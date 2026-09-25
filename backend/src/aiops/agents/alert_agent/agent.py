"""Alert Agent (UC-06).

Investigation loop:
  1. deterministic tool calls: alerts for the service's labels (all states), alerts for
     each ``depends_on`` service, and silences that could apply to the service;
  2. deterministic analysis: what is firing (vs silenced/inhibited, vs started after the
     window), severity, and each alert's start relative to the incident start;
  3. bounded LLM follow-ups + an evidence-cited report;
  4. finalize: signals and status come from the data (an LLM can neither invent nor
     hide an alert), "no active alerts" is always said explicitly, and an alert is never
     reported as a root cause.

Vendor-neutral: alert label identifiers from the service catalog (``alerts.labels``),
label names, severities and the UI link template from the ``alerts`` capability settings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from aiops.agents.alert_agent.analysis import (
    NO_ALERTS_PHRASE,
    SIGNALS,
    AlertAnalysis,
    analyze,
    iso,
    parse_ts,
)
from aiops.agents.base import AgentRun, AgentSpec, BaseAgent
from aiops.agents.registry import AGENTS
from aiops.core.config import ConfigError
from aiops.core.models import AgentResult, AgentStatus, ClaimKind, Evidence, EvidenceKind
from aiops.llm.base import ToolSpec

DEFAULT_CRITICAL = ["critical"]
SERVICE_ALERT_LIMIT = 50
DEPENDENCY_ALERT_LIMIT = 20
SILENCE_LIMIT = 20
#: Hint keys (set by the orchestrator from other agents) that move the correlation anchor.
INCIDENT_START_HINTS = ("incident_start", "first_error_at")


@dataclass(frozen=True)
class AlertScope:
    service: str
    labels: dict[str, str]
    dependencies: dict[str, dict[str, str]]  # name -> alert labels
    critical_severities: list[str]
    link_template: str | None


def matcher_filter(labels: dict[str, str]) -> str:
    """Alertmanager UI filter syntax: {service="payment-service",namespace="prod"}."""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


class AlertAgent(BaseAgent):
    spec = AgentSpec(
        name="alerts",
        version="1",
        description="Checks firing alerts for a service and its dependencies, silences, and when alerts started vs the incident.",
        capabilities=["alerts"],
        evidence_kind=EvidenceKind.ALERT,
        prompt="alerts",
    )

    # -- scope ---------------------------------------------------------------------------

    def scope(self, run: AgentRun) -> AlertScope:
        ctx = run.task.context
        if not ctx.service:
            raise LookupError("No service given: the Alert agent needs a catalog service.")
        settings = self.deps.settings.capability("alerts").settings
        service_label = str(settings.get("labels", {}).get("service", "service"))

        def labels_for(name: str) -> dict[str, str]:
            try:
                entry = self.deps.catalog.get(name)
            except ConfigError:  # not a catalog service (postgres, redis): label by name
                return {service_label: name}
            labels = entry.identifiers("alerts", ctx.environment).get("labels")
            return {str(k): str(v) for k, v in (labels or {service_label: name}).items()}

        entry = self.deps.catalog.get(ctx.service)
        return AlertScope(
            service=ctx.service,
            labels=labels_for(ctx.service),
            dependencies={dep: labels_for(dep) for dep in entry.depends_on},
            critical_severities=list(settings.get("critical_severities", DEFAULT_CRITICAL)),
            link_template=settings.get("ui_link_template"),
        )

    def ui_link(self, scope: AlertScope, labels: dict[str, str]) -> str | None:
        if not scope.link_template:
            return None
        return scope.link_template.format(filter=quote(matcher_filter(labels), safe=""))

    def incident_start(self, run: AgentRun) -> tuple[datetime, str]:
        window = run.task.context.time_range
        for key in INCIDENT_START_HINTS:
            ts = parse_ts(run.task.hints.get(key))
            if ts is not None and window.start <= ts <= window.end:
                return ts, f"hint '{key}'"
        return window.start, "window start"

    # -- deterministic phase ---------------------------------------------------------------

    async def _list(
        self, run: AgentRun, labels: dict[str, str], limit: int, summary: str
    ) -> tuple[Any, Evidence | None, str | None]:
        outcome, evidence = await run.call_tool(
            "list_alerts", {"labels": labels, "state": "all", "limit": limit}, summary=summary
        )
        return outcome.data, evidence, outcome.tool_call.error

    async def deterministic(
        self, run: AgentRun, scope: AlertScope
    ) -> tuple[AlertAnalysis | None, list[str]]:
        notes: list[str] = []
        service_data, service_ev, err = await self._list(
            run, scope.labels, SERVICE_ALERT_LIMIT, f"Alerts for {scope.service}"
        )
        if service_ev is None:
            return None, [f"Alert query failed: {err}"]

        dependency_data: dict[str, Any] = {}
        dependency_ev: dict[str, Evidence] = {}
        failed: list[str] = []
        for dep, labels in scope.dependencies.items():
            data, evidence, err = await self._list(
                run, labels, DEPENDENCY_ALERT_LIMIT, f"Alerts for dependency {dep}"
            )
            if evidence is None:
                failed.append(dep)
                notes.append(f"Alert query for {dep} failed: {err}")
                continue
            dependency_data[dep] = data
            dependency_ev[dep] = evidence

        outcome, silences_ev = await run.call_tool(
            "list_silences",
            {"labels": scope.labels, "state": "active", "limit": SILENCE_LIMIT},
            summary=f"Active silences that could apply to {scope.service}",
        )
        if silences_ev is None:
            notes.append(f"Silence query failed: {outcome.tool_call.error}")

        start, source = self.incident_start(run)
        analysis = analyze(
            service_data,
            dependency_data,
            outcome.data if silences_ev else None,
            service=scope.service,
            window=run.task.context.time_range,
            incident_start=start,
            incident_start_source=source,
            critical_severities=scope.critical_severities,
            failed_scopes=failed,
        )

        # Evidence summaries, links and timestamps now that the alerts are classified.
        self._describe(service_ev, analysis, scope.service, scope.labels, scope)
        notes.append(f"[{service_ev.id}] service_alerts")
        for dep, evidence in dependency_ev.items():
            self._describe(evidence, analysis, dep, scope.dependencies[dep], scope)
            notes.append(f"[{evidence.id}] dependency_alerts ({dep})")
        if silences_ev is not None:
            count = len(analysis.silences)
            silences_ev.summary = (
                f"{count} active silence(s) could apply to {scope.service}"
                if count
                else f"No active silences for {scope.service}"
            )
            silences_ev.link = self.ui_link(scope, scope.labels)
            notes.append(f"[{silences_ev.id}] silences")
        return analysis, notes

    def _describe(
        self,
        evidence: Evidence,
        analysis: AlertAnalysis,
        scope_name: str,
        labels: dict[str, str],
        scope: AlertScope,
    ) -> None:
        mine = [a for a in analysis.alerts if a.scope == scope_name]
        firing = [a for a in analysis.firing if a.scope == scope_name]
        if firing:
            parts = [
                f"{a.alertname} ({a.severity}, since {iso(a.starts_at) if a.starts_at else '?'})"
                for a in firing
            ]
            evidence.summary = f"{len(firing)} firing alert(s) for {scope_name}: " + "; ".join(
                parts
            )
        else:
            evidence.summary = f"{NO_ALERTS_PHRASE.lower()} for {scope_name}".capitalize()
        muted = len(mine) - len(firing)
        if muted:
            evidence.summary += f" ({muted} suppressed or started after the window)"
        starts = [a.starts_at for a in firing if a.starts_at is not None]
        evidence.timestamp = min(starts) if starts else None
        evidence.link = self.ui_link(scope, labels)

    # -- investigation -------------------------------------------------------------------

    def prompt_variables(self, run: AgentRun) -> dict[str, object]:
        variables = super().prompt_variables(run)
        scope = self.scope(run)
        window = run.task.context.time_range
        variables.update(
            start=iso(window.start),
            end=iso(window.end),
            labels=json.dumps(scope.labels, sort_keys=True),
            dependencies=", ".join(scope.dependencies) or "none",
            critical_severities=", ".join(scope.critical_severities),
            signals=", ".join(f"`{s}`" for s in SIGNALS),
        )
        return variables

    async def investigate(self, run: AgentRun, tool_specs: list[ToolSpec]) -> AgentResult:
        try:
            scope = self.scope(run)
        except (LookupError, ConfigError) as exc:
            return run.failed(str(exc))
        analysis, notes = await self.deterministic(run, scope)
        if analysis is None:
            return run.failed("Could not query alerts: " + " ".join(notes))

        system, user = self.build_prompt(run)
        overview = "\n".join([*analysis.lines(), "Evidence ids: " + "; ".join(notes)])
        user = f"{user}\n\n## Overview (computed for you, deterministic)\n{overview}"
        result = await self.llm_loop(run, tool_specs, system, user)
        return self.finalize(result, analysis, scope)

    def finalize(
        self, result: AgentResult, analysis: AlertAnalysis, scope: AlertScope
    ) -> AgentResult:
        """Data decides signals and status; alerts stay evidence, never a root cause."""
        if result.status not in (AgentStatus.SUCCESS, AgentStatus.NO_SIGNAL):
            return result  # partial/failed: keep the reason, don't invent signals
        firing = bool(analysis.firing)
        status = AgentStatus.SUCCESS if firing else AgentStatus.NO_SIGNAL
        summary = result.summary
        if not firing and NO_ALERTS_PHRASE.casefold() not in summary.casefold():
            summary = f"{analysis.no_alerts_sentence()} {summary}".strip()
        findings = [
            f.model_copy(update={"kind": ClaimKind.HYPOTHESIS})
            if f.kind is ClaimKind.FACT and "root_cause" in f.type
            else f
            for f in result.findings
        ]
        evidence = [
            e if e.link else e.model_copy(update={"link": self.ui_link(scope, scope.labels)})
            for e in result.evidence
        ]
        return result.model_copy(
            update={
                "signals": analysis.signals,
                "status": status,
                "summary": summary,
                "findings": findings,
                "evidence": evidence,
            }
        )


AGENTS.register(AlertAgent)
