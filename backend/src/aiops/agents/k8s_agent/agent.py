"""K8s Agent (UC-07): Kubernetes workload health for a service.

Investigation loop:
  1. deterministic tool calls: the deployment with its rollout history, the service's
     pods, the Warning events of its objects in the window, and (optionally) the
     dependencies' deployments from the catalog's ``depends_on``;
  2. deterministic analysis: replica status, restarts, OOM kills, crash loops, image
     pull errors, probe failures (start-up noise excluded), rollouts inside the window
     (current vs previous revision, image, change-cause), unavailable dependencies;
  3. bounded LLM follow-ups (e.g. get_pod_logs previous=true) + an evidence-cited report;
  4. finalize: signals and status come from the data, a data-derived headline leads the
     summary, each signal gets a FACT/OBSERVATION finding citing its evidence, and a
     rollout is never asserted as a root cause.

Vendor-neutral: deployment, label selector, container and namespace come from the
service catalog (``k8s``); the namespace default, thresholds and link template from
the ``k8s`` capability settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from aiops.agents.base import AgentRun, AgentSpec, BaseAgent
from aiops.agents.k8s_agent.analysis import (
    PROBLEM_SIGNALS,
    SIGNALS,
    K8sAnalysis,
    analyze,
    iso,
    parse_ts,
)
from aiops.agents.registry import AGENTS
from aiops.core.config import ConfigError
from aiops.core.models import (
    AgentResult,
    AgentStatus,
    ClaimKind,
    Evidence,
    EvidenceKind,
    Finding,
)
from aiops.llm.base import ToolSpec

HISTORY = 5
POD_LIMIT = 50
EVENT_LIMIT = 100
DEPLOYMENT_LIMIT = 50
#: Hint keys (set by the orchestrator from other agents) that move the correlation anchor.
INCIDENT_START_HINTS = ("incident_start", "first_error_at")


@dataclass(frozen=True)
class K8sScope:
    service: str
    namespace: str
    deployment: str
    label_selector: str
    container: str | None
    dependencies: dict[str, str]  # dependency name -> deployment name (same namespace)
    skipped_dependencies: list[str]
    link_template: str | None
    probe_startup_grace_s: float


class K8sAgent(BaseAgent):
    spec = AgentSpec(
        name="k8s",
        version="1",
        description="Checks a service's Kubernetes workload: replicas, pod restarts, OOM kills, "
        "image pull errors, Warning events, rollout history and dependency deployments.",
        capabilities=["k8s"],
        evidence_kind=EvidenceKind.K8S_EVENT,
        prompt="k8s",
    )

    # -- scope ---------------------------------------------------------------------------

    def scope(self, run: AgentRun) -> K8sScope:
        ctx = run.task.context
        if not ctx.service:
            raise LookupError("No service given: the K8s agent needs a catalog service.")
        settings = self.deps.settings.capability("k8s").settings
        default_ns = str(settings.get("default_namespace", "default"))
        entry = self.deps.catalog.get(ctx.service)
        ids = entry.identifiers("k8s", ctx.environment)
        namespace = str(ids.get("namespace") or default_ns)
        deployment = str(ids.get("deployment") or ctx.service)

        dependencies: dict[str, str] = {}
        skipped: list[str] = []
        if settings.get("include_dependencies", True):
            for dep in entry.depends_on:
                try:
                    dep_ids = self.deps.catalog.get(dep).identifiers("k8s", ctx.environment)
                except ConfigError:  # not a catalog service (postgres, redis): deployment = name
                    dependencies[dep] = dep
                    continue
                if str(dep_ids.get("namespace") or default_ns) != namespace:
                    skipped.append(dep)  # another namespace: out of this agent's scope
                    continue
                dependencies[dep] = str(dep_ids.get("deployment") or dep)
        return K8sScope(
            service=ctx.service,
            namespace=namespace,
            deployment=deployment,
            label_selector=str(ids.get("label_selector") or f"app={deployment}"),
            container=ids.get("container"),
            dependencies=dependencies,
            skipped_dependencies=skipped,
            link_template=settings.get("ui_link_template") or None,
            probe_startup_grace_s=float(settings.get("probe_startup_grace_s", 60)),
        )

    def link(self, scope: K8sScope, kind: str, name: str) -> str | None:
        if not scope.link_template:
            return None
        return scope.link_template.format(
            namespace=quote(scope.namespace), kind=quote(kind), name=quote(name)
        )

    def incident_start(self, run: AgentRun) -> tuple[datetime, str]:
        window = run.task.context.time_range
        for key in INCIDENT_START_HINTS:
            ts = parse_ts(run.task.hints.get(key))
            if ts is not None and window.start <= ts <= window.end:
                return ts, f"hint '{key}'"
        return window.start, "window start"

    # -- deterministic phase ---------------------------------------------------------------

    async def _call(
        self,
        run: AgentRun,
        tool: str,
        args: dict[str, Any],
        summary: str,
        failed: list[str],
        part: str,
    ) -> tuple[Any, Evidence | None]:
        outcome, evidence = await run.call_tool(tool, args, summary=summary)
        if evidence is None:
            failed.append(f"{part} ({outcome.tool_call.error})")
            return None, None
        return outcome.data, evidence

    async def deterministic(
        self, run: AgentRun, scope: K8sScope
    ) -> tuple[K8sAnalysis, dict[str, Evidence]]:
        window = run.task.context.time_range
        ns, name = scope.namespace, scope.deployment
        failed: list[str] = []
        evidence: dict[str, Evidence] = {}

        dep_data, ev = await self._call(
            run,
            "get_deployment",
            {"namespace": ns, "name": name, "history": HISTORY},
            f"Deployment {ns}/{name} and its rollout history",
            failed,
            "deployment",
        )
        if ev:
            evidence["deployment"] = ev
        pods_data, ev = await self._call(
            run,
            "list_pods",
            {"namespace": ns, "label_selector": scope.label_selector, "limit": POD_LIMIT},
            f"Pods of {name}",
            failed,
            "pods",
        )
        if ev:
            evidence["pods"] = ev
        events_data, ev = await self._call(
            run,
            "list_events",
            {
                "namespace": ns,
                "object_name_prefix": name,
                "type": "Warning",
                "since": iso(window.start),
                "limit": EVENT_LIMIT,
            },
            f"Warning events for {name} since {iso(window.start)}",
            failed,
            "events",
        )
        if ev:
            evidence["events"] = ev
        deps_data = None
        if scope.dependencies:
            deps_data, ev = await self._call(
                run,
                "list_deployments",
                {"namespace": ns, "limit": DEPLOYMENT_LIMIT},
                f"Dependency deployments of {scope.service}",
                failed,
                "dependencies",
            )
            if ev:
                evidence["dependencies"] = ev

        start, source = self.incident_start(run)
        analysis = analyze(
            dep_data,
            pods_data,
            events_data,
            deps_data,
            service=scope.service,
            deployment=name,
            namespace=ns,
            dependencies=scope.dependencies,
            window=window,
            incident_start=start,
            incident_start_source=source,
            probe_startup_grace_s=scope.probe_startup_grace_s,
            failed=failed,
        )
        if dep_data is not None and not analysis.found:
            analysis.failed.append("deployment (not found)")
        self._describe(evidence, analysis, scope)
        return analysis, evidence

    def _describe(
        self, evidence: dict[str, Evidence], analysis: K8sAnalysis, scope: K8sScope
    ) -> None:
        """Evidence summaries, timestamps and links, now that the data is classified."""
        name = scope.deployment
        if dep := evidence.get("deployment"):
            rollout = analysis.rollouts[0] if analysis.rollouts else None
            dep.summary = (
                f"Deployment {name}: {analysis.ready}/{analysis.desired} ready, revision "
                f"{analysis.revision} ('{analysis.change_cause or '-'}')"
            )
            if rollout and rollout.revision.created_at:
                dep.summary += f"; rolled out at {iso(rollout.revision.created_at)}"
                dep.timestamp = rollout.revision.created_at
            dep.link = self.link(scope, "Deployment", name)
        if pods := evidence.get("pods"):
            restarts = sum(c.restart_count for c in analysis.containers)
            reasons = sorted(
                {c.last_reason for c in analysis.oom_killed if c.last_reason}
                | {c.waiting_reason for c in analysis.containers if c.waiting_reason}
            )
            pods.summary = f"{len(analysis.pods)} pod(s) of {name}, {restarts} restart(s)" + (
                f", {', '.join(reasons)}" if reasons else ""
            )
            finished = [c.last_finished_at for c in analysis.restarted if c.last_finished_at]
            pods.timestamp = max(finished) if finished else None
            pods.link = self.link(scope, "Deployment", name)
        if events := evidence.get("events"):
            warnings = [e for e in analysis.events if e.type == "Warning"]
            by_reason: dict[str, int] = {}
            for e in warnings:
                by_reason[e.reason] = by_reason.get(e.reason, 0) + e.count
            events.summary = (
                f"Warning events for {name}: "
                + ", ".join(f"{r} x{n}" for r, n in sorted(by_reason.items()))
                if warnings
                else f"No Warning events for {name} in the window"
            )
            seen = [e.first_seen for e in warnings if e.first_seen]
            events.timestamp = min(seen) if seen else None
        if deps := evidence.get("dependencies"):
            deps.summary = "Dependencies: " + "; ".join(d.state() for d in analysis.dependencies)
            deps.data = {
                "dependencies": [
                    {
                        "name": d.name,
                        "found": d.found,
                        "desired": d.desired,
                        "ready": d.ready,
                        "revision": d.revision,
                        "change_cause": d.change_cause,
                        "last_rollout": iso(d.last_rollout) if d.last_rollout else None,
                    }
                    for d in analysis.dependencies
                ]
            }

    # -- investigation -------------------------------------------------------------------

    def prompt_variables(self, run: AgentRun) -> dict[str, object]:
        variables = super().prompt_variables(run)
        scope = self.scope(run)
        window = run.task.context.time_range
        variables.update(
            start=iso(window.start),
            end=iso(window.end),
            namespace=scope.namespace,
            deployment=scope.deployment,
            label_selector=scope.label_selector,
            container=scope.container or "app",
            dependencies=", ".join(scope.dependencies) or "none",
            signals=", ".join(f"`{s}`" for s in SIGNALS),
        )
        return variables

    async def investigate(self, run: AgentRun, tool_specs: list[ToolSpec]) -> AgentResult:
        try:
            scope = self.scope(run)
        except (LookupError, ConfigError) as exc:
            return run.failed(str(exc))
        analysis, evidence = await self.deterministic(run, scope)
        if not evidence:
            return run.failed("Could not query Kubernetes: " + "; ".join(analysis.failed))

        system, user = self.build_prompt(run)
        ids = "; ".join(f"[{ev.id}] {label}" for label, ev in evidence.items())
        overview = "\n".join([*analysis.lines(), f"Evidence ids: {ids}"])
        user = f"{user}\n\n## Overview (computed for you, deterministic)\n{overview}"
        result = await self.llm_loop(run, tool_specs, system, user)
        return self.finalize(result, analysis, evidence)

    # -- finalize ------------------------------------------------------------------------

    def data_findings(self, analysis: K8sAnalysis, evidence: dict[str, Evidence]) -> list[Finding]:
        """One finding per signal, citing the evidence it was derived from."""

        def cite(*labels: str) -> list[str]:
            return [evidence[label].id for label in labels if label in evidence]

        findings: list[Finding] = []

        def add(kind: ClaimKind, type_: str, text: str, *labels: str) -> None:
            ids = cite(*labels)
            if ids:
                findings.append(Finding(kind=kind, type=type_, description=text, evidence_ids=ids))

        signals = set(analysis.signals)
        for r in analysis.rollouts:
            add(
                ClaimKind.FACT,
                "recent_rollout",
                r.line(analysis.incident_start).strip(" -"),
                "deployment",
            )
        if "oom_killed" in signals:
            items = [
                f"container {c.name} of {c.pod} was OOMKilled (exit {c.last_exit_code})"
                + (f" at {iso(c.last_finished_at)}" if c.last_finished_at else "")
                for c in analysis.oom_killed
            ] or [f"{e.reason} on {e.object}: {e.message}" for e in analysis.oom_events]
            add(ClaimKind.FACT, "oom_killed", "; ".join(items), "pods", "events")
        if "pod_restarts" in signals:
            total = sum(c.restart_count for c in analysis.restarted)
            pods = ", ".join(sorted({c.pod for c in analysis.restarted}))
            add(ClaimKind.FACT, "pod_restarts", f"{total} container restart(s) in {pods}", "pods")
        if "crash_loop" in signals:
            add(
                ClaimKind.FACT,
                "crash_loop",
                "Containers in CrashLoopBackOff / Back-off restarting failed container: "
                + ", ".join(
                    sorted(
                        {c.pod for c in analysis.crash_looping}
                        | {e.object for e in analysis.backoff_events}
                    )
                ),
                "pods",
                "events",
            )
        if "image_pull_error" in signals:
            items = [
                f"{c.pod} waiting {c.waiting_reason} for image {c.image}"
                for c in analysis.pull_errors
            ] or [f"{e.reason} on {e.object}: {e.message}" for e in analysis.pull_events]
            add(ClaimKind.FACT, "image_pull_error", "; ".join(items), "pods", "events")
        if "replicas_unavailable" in signals:
            add(
                ClaimKind.FACT,
                "replicas_unavailable",
                f"Deployment {analysis.deployment} has {analysis.ready}/{analysis.desired} replicas "
                f"ready ({analysis.available} available, {analysis.unavailable} unavailable, "
                f"{analysis.updated} updated to the current revision)",
                "deployment",
            )
        if "probe_failures" in signals:
            add(
                ClaimKind.OBSERVATION,
                "probe_failures",
                "; ".join(
                    f"{e.object}: {e.message} (x{e.count})" for e in analysis.probe_events[:3]
                ),
                "events",
            )
        for d in analysis.unavailable_dependencies:
            add(ClaimKind.FACT, "dependency_unavailable", f"Dependency {d.state()}", "dependencies")
        for d in analysis.dependency_rollouts:
            add(
                ClaimKind.FACT,
                "dependency_rollout",
                f"Dependency {d.name} completed a rollout to revision {d.revision} "
                f"('{d.change_cause or '-'}') at {iso(d.last_rollout) if d.last_rollout else '?'}",
                "dependencies",
            )
        if "healthy" in signals and not set(PROBLEM_SIGNALS) & signals:
            add(
                ClaimKind.OBSERVATION,
                "healthy",
                f"{analysis.deployment}: {analysis.ready}/{analysis.desired} replicas ready, "
                "no restarts, OOM kills, pull errors or sustained probe failures in the window",
                "pods",
                "deployment",
            )
        return findings

    def finalize(
        self, result: AgentResult, analysis: K8sAnalysis, evidence: dict[str, Evidence]
    ) -> AgentResult:
        """Data decides signals and status; a rollout stays a correlation, not a cause."""
        if result.status not in (AgentStatus.SUCCESS, AgentStatus.NO_SIGNAL):
            return result  # partial/failed: keep the reason, don't invent signals
        status = AgentStatus.SUCCESS if analysis.problem else AgentStatus.NO_SIGNAL
        headline = analysis.headline()
        summary = result.summary
        if headline.casefold() not in summary.casefold():
            summary = f"{headline} {summary}".strip()
        llm_findings = [
            f.model_copy(update={"kind": ClaimKind.HYPOTHESIS})
            if f.kind is ClaimKind.FACT and "root_cause" in f.type
            else f
            for f in result.findings
        ]
        covered = {f.type for f in llm_findings}
        findings = [
            *[f for f in self.data_findings(analysis, evidence) if f.type not in covered],
            *llm_findings,
        ]
        return result.model_copy(
            update={
                "signals": analysis.signals,
                "status": status,
                "summary": summary,
                "findings": findings,
            }
        )


AGENTS.register(K8sAgent)
