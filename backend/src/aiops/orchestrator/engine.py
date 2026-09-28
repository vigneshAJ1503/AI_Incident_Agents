"""The orchestrator (PR-030/031, MASTER_PLAN §12): ONE investigation from 7 agents.

    question -> planner -> round 1 (parallel) -> gap analysis -> round 2 (parallel)
             -> RCA -> response builder

Executor rules:
  * ``asyncio`` with a concurrency limit (``orchestrator.max_concurrency``), a wall-clock
    timeout per step, and cancellation (``Orchestrator.cancel``);
  * one failing agent never crashes the investigation: its step fails, the others go
    on and the investigation becomes PARTIAL (the report says what's missing);
  * a token budget per investigation (``orchestrator.max_tokens``): steps that would
    start after it is spent are skipped (PARTIAL);
  * every state change is published on the ``EventBus`` with exactly the SSE event
    types of docs/api/contract.md.

Vendor-neutral: agents come from the registry, filtered by the profile's enabled
capabilities; nothing here knows about Elasticsearch, Prometheus or Jira.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import aiops.agents  # noqa: F401  (registers built-in agents)
from aiops.agents.base import AgentDeps
from aiops.agents.deps import build_deps
from aiops.agents.registry import AGENTS, AgentRegistry
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import ConfigError, Settings
from aiops.core.events import CallbackEventSink, Event, EventBus, EventSink
from aiops.core.guardrails.audit import AuditSink
from aiops.core.models import (
    AgentResult,
    AgentStatus,
    AgentTask,
    Incident,
    IncidentContext,
    Investigation,
    InvestigationStatus,
    InvestigationStep,
    StepStatus,
    utcnow,
)
from aiops.evals.replay import echo_responder
from aiops.llm.base import LLMProvider
from aiops.llm.factory import create_provider
from aiops.llm.fake import FakeLLMProvider
from aiops.mcp.registry import MCPRegistry
from aiops.orchestrator.gaps import GapAnalysis, analyze_gaps
from aiops.orchestrator.planner import Plan, Planner, PlanRequest
from aiops.orchestrator.replay import ReplaySource, replay_llm_enabled, shift_result

log = logging.getLogger(__name__)

STEP_TIMEOUT_MARGIN_S = 15.0
Mode = Literal["live", "replay", "demo"]
#: Called once the rounds are done: fills hypotheses, recommendations, timeline, report.
Concluder = Callable[["InvestigationRun"], Awaitable[None]]


@dataclass
class InvestigationRequest:
    question: str
    service: str | None = None
    environment: str | None = None
    since: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    mode: Mode = "live"


@dataclass
class InvestigationRun:
    """Mutable state of one investigation while it runs."""

    investigation: Investigation
    plan: Plan | None = None
    gaps: GapAnalysis | None = None
    missing: list[str] = field(default_factory=list)  # what couldn't be checked (PARTIAL)
    notes: list[str] = field(default_factory=list)
    cancelled: bool = False
    tasks: set[asyncio.Task[AgentResult | None]] = field(default_factory=set)
    started: float = field(default_factory=time.perf_counter)

    @property
    def id(self) -> str:
        return self.investigation.id

    def spent(self) -> int:
        return self.investigation.usage.total_tokens


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLMProvider | None = None,
        bus: EventBus | None = None,
        registry: AgentRegistry = AGENTS,
        catalog: ServiceCatalog | None = None,
        replay: ReplaySource | None = None,
        audit: AuditSink | None = None,
        concluder: Concluder | None = None,
        deps_factory: Callable[[str, str | None, EventSink], AgentDeps] | None = None,
    ) -> None:
        self.settings = settings
        self.config = settings.orchestrator
        #: No usable LLM (replay, or no key configured): agents submit their deterministic
        #: overview and the planner/RCA stay rule-based. Zero tokens.
        self.deterministic_only = False
        #: AIOPS_REPLAY_LLM=real: recorded data, but agents/planner/RCA reason with the
        #: configured (hosted) LLM. Lets a real LLM be exercised without the live stack.
        replay_with_llm = replay is not None and replay_llm_enabled()
        if llm is None and (replay is None or replay_with_llm):
            try:
                llm = create_provider(settings.llm)
            except ConfigError as exc:
                log.warning("No LLM configured (%s): deterministic mode", exc)
                llm = None
        if replay is not None and replay_with_llm and llm is not None:
            replay.llm = llm
        if llm is None:
            self.deterministic_only = True
            llm = FakeLLMProvider(responder=echo_responder())
        self.llm = llm
        self.bus = bus or EventBus()
        self.registry = registry
        self.catalog = catalog or ServiceCatalog.from_settings(settings)
        self.replay = replay
        self.audit = audit
        self.concluder = concluder
        self._deps_factory = deps_factory
        self._runs: dict[str, InvestigationRun] = {}
        # Replays plan with the deterministic parser only (zero tokens, no key needed).
        self.planner = Planner(
            settings, self.catalog, registry, llm=None if self.deterministic_only else self.llm
        )

    # -- public API --------------------------------------------------------------------------

    async def plan(self, request: InvestigationRequest) -> Plan:
        return await self.planner.plan(self._plan_request(request))

    def running(self, investigation_id: str) -> Investigation | None:
        """The live (mutable) state of a running investigation, e.g. for the API's GET."""
        run = self._runs.get(investigation_id)
        return run.investigation if run else None

    def cancel(self, investigation_id: str) -> bool:
        run = self._runs.get(investigation_id)
        if run is None:
            return False
        run.cancelled = True
        for task in list(run.tasks):
            task.cancel()
        return True

    async def investigate(
        self, request: InvestigationRequest, *, investigation_id: str | None = None
    ) -> Investigation:
        question = self._plan_request(request).question.strip()
        incident = Incident(title=question[:200] or "Incident")
        investigation = Investigation(
            incident=incident, status=InvestigationStatus.RUNNING, mode=request.mode
        )
        if investigation_id:
            investigation.id = investigation_id
        run = InvestigationRun(investigation)
        self._runs[run.id] = run
        self._publish(run, "investigation_started", question=question)
        try:
            await self._investigate(run, request)
        except asyncio.CancelledError:
            run.cancelled = True
        except Exception as exc:  # the orchestrator reports, never crashes the caller
            log.exception("Investigation %s crashed", run.id)
            investigation.status = InvestigationStatus.FAILED
            self._publish(run, "error", message=f"{type(exc).__name__}: {exc}", recoverable=False)
        finally:
            self._runs.pop(run.id, None)
        if run.cancelled:
            investigation.status = InvestigationStatus.CANCELLED
            self._publish(run, "error", message="Investigation cancelled", recoverable=False)
        return self._finish(run)

    # -- the flow ----------------------------------------------------------------------------

    def _plan_request(self, request: InvestigationRequest) -> PlanRequest:
        if self.replay is not None:
            base = self.replay.request()
            base.question = request.question or base.question
            return base
        return PlanRequest(
            question=request.question,
            service=request.service,
            environment=request.environment,
            since=request.since,
            start=request.start,
            end=request.end,
        )

    async def _investigate(self, run: InvestigationRun, request: InvestigationRequest) -> None:
        inv = run.investigation
        plan = await self.planner.plan(self._plan_request(request))
        run.plan = plan
        run.notes.extend(plan.notes)
        inv.usage = inv.usage + plan.usage
        inv.context = plan.context
        inv.incident = inv.incident.model_copy(
            update={"service": plan.context.service, "environment": plan.context.environment}
        )
        if plan.needs_clarification:
            inv.status = InvestigationStatus.NEEDS_CLARIFICATION
            inv.clarification_question = plan.clarification_question
            inv.clarification_candidates = plan.candidates
            self._publish(
                run,
                "clarification_needed",
                question=plan.clarification_question,
                candidates=plan.candidates,
            )
            return

        inv.steps = list(plan.steps)
        self._publish(
            run,
            "plan_created",
            context=plan.context.model_dump(mode="json"),
            steps=[s.model_dump(mode="json") for s in inv.steps],
        )

        round1 = plan.round(1)
        results1 = await self._run_round(run, 1, round1, lambda s: self._round1_task(run, s))
        if run.cancelled:
            return
        if round1 and all(r is None or r.status is AgentStatus.FAILED for r in results1):
            run.missing.append("every round-1 agent failed")

        if self.config.max_rounds >= 2 and inv.context is not None:
            await self._round2(run, [r for r in results1 if r is not None])
            if run.cancelled:
                return

        if self.concluder is not None:
            await self.concluder(run)
        else:  # RCA (PR-033) + response builder (PR-034)
            from aiops.orchestrator.conclude import conclude

            await conclude(self, run)

    async def _round2(self, run: InvestigationRun, results1: list[AgentResult]) -> None:
        inv = run.investigation
        context = inv.context
        if context is None or context.service is None:
            return
        enabled = {s.name for s in self.planner.enabled_agents()}
        gaps = analyze_gaps(
            results1,
            service=context.service,
            catalog=self.catalog,
            enabled_agents=enabled,
            round2_agents=set(self.config.round2_agents),
            overrides=self.config.followups,
        )
        run.gaps = gaps
        run.notes.extend(gaps.skipped)
        # The investigation's symptoms are enriched with round-1 signals.
        inv.context = context.model_copy(
            update={"symptoms": list(dict.fromkeys([*context.symptoms, *gaps.signals]))}
        )
        round1_ids = [s.id for s in inv.steps if s.round == 1]
        followups = [
            InvestigationStep(
                agent=f.agent,
                objective=self.planner.objective(
                    self.registry.get(f.agent).spec, f.service, f"{f.objective} ({f.reason})"
                ),
                round=2,
                depends_on=round1_ids,
            )
            for f in gaps.followups
        ]
        targets = {step.id: f.service for step, f in zip(followups, gaps.followups, strict=True)}
        inv.steps.extend(followups)
        round2 = [s for s in inv.steps if s.round == 2]
        if not round2:
            return

        def task_for(step: InvestigationStep) -> AgentTask:
            return self._round2_task(run, step, targets.get(step.id))

        await self._run_round(run, 2, round2, task_for)

    # -- tasks -------------------------------------------------------------------------------

    def _task(
        self,
        run: InvestigationRun,
        step: InvestigationStep,
        context: IncidentContext,
        hints: dict[str, Any] | None = None,
    ) -> AgentTask:
        return AgentTask(
            id=step.id,  # task id == step id: agent events map straight onto steps
            investigation_id=run.id,
            agent=step.agent,
            objective=step.objective,
            context=context,
            hints=hints or {},
            round=step.round,
        )

    def _round1_task(self, run: InvestigationRun, step: InvestigationStep) -> AgentTask:
        return self._task(run, step, self._context(run))

    def _round2_task(
        self, run: InvestigationRun, step: InvestigationStep, service: str | None
    ) -> AgentTask:
        context = self._context(run)
        gaps = run.gaps or GapAnalysis()
        plan_symptoms = run.plan.context.symptoms if run.plan else []
        if service is not None and service != context.service:
            # A follow-up on a dependency: same question and window, that service.
            return self._task(run, step, context.model_copy(update={"service": service}))
        if step.agent == "knowledge":
            # Searches are built from structured hints; question words are searched anyway.
            ctx = context.model_copy(update={"symptoms": []})
            return self._task(run, step, ctx, gaps.knowledge_hints())
        if step.agent == "tickets":
            # Round-1 signals are the symptoms tickets are matched against.
            return self._task(run, step, context.model_copy(update={"symptoms": gaps.signals}))
        return self._task(
            run,
            step,
            context.model_copy(update={"symptoms": plan_symptoms}),
            {"signals": gaps.signals},
        )

    @staticmethod
    def _context(run: InvestigationRun) -> IncidentContext:
        context = run.investigation.context
        if context is None:
            raise RuntimeError("the investigation has no context before planning")
        return context

    # -- execution ---------------------------------------------------------------------------

    async def _run_round(
        self,
        run: InvestigationRun,
        number: int,
        steps: list[InvestigationStep],
        task_for: Callable[[InvestigationStep], AgentTask],
    ) -> list[AgentResult | None]:
        if not steps:
            return []
        self._publish(run, "round_started", round=number, agents=[s.agent for s in steps])
        semaphore = asyncio.Semaphore(self.config.max_concurrency)
        tasks: list[asyncio.Task[AgentResult | None]] = []
        for step in steps:
            task = task_for(step)
            tasks.append(asyncio.create_task(self._run_step(run, step, task, semaphore)))
        run.tasks.update(tasks)
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        run.tasks.difference_update(tasks)
        results: list[AgentResult | None] = []
        for step, outcome in zip(steps, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                self._close_step(step, StepStatus.FAILED)
                reason = (
                    "cancelled" if isinstance(outcome, asyncio.CancelledError) else str(outcome)
                )
                run.missing.append(f"{step.agent}: {reason}")
                results.append(None)
            else:
                results.append(outcome)
        return results

    def _deps(self, agent: str, service: str | None, events: EventSink) -> AgentDeps:
        if self._deps_factory is not None:
            return self._deps_factory(agent, service, events)
        if self.replay is not None:
            return self.replay.deps(self.settings, agent, service, events)
        mcp = MCPRegistry(self.settings, audit=self.audit) if self.audit else None
        return build_deps(self.settings, llm=self.llm, mcp=mcp, events=events)

    def _step_timeout(self, agent: str) -> float:
        if self.config.step_timeout_s is not None:
            return self.config.step_timeout_s
        return self.settings.agent_limits(agent).max_execution_s + STEP_TIMEOUT_MARGIN_S

    async def _run_step(
        self,
        run: InvestigationRun,
        step: InvestigationStep,
        task: AgentTask,
        semaphore: asyncio.Semaphore,
    ) -> AgentResult | None:
        inv = run.investigation
        offset = timedelta(0)
        if self.replay is not None:
            if self.replay.fixture_dir(step.agent, task.context.service) is None:
                self._close_step(step, StepStatus.SKIPPED)
                self._publish(
                    run,
                    "error",
                    agent=step.agent,
                    message=(
                        f"Replay: no recorded data for {step.agent} on {task.context.service}; "
                        "step skipped."
                    ),
                    recoverable=True,
                )
                return None
            task, offset = self.replay.prepare(task)

        async with semaphore:
            if run.cancelled:
                self._close_step(step, StepStatus.SKIPPED)
                return None
            if run.spent() >= self.config.max_tokens:
                self._close_step(step, StepStatus.SKIPPED)
                run.missing.append(f"{step.agent}: token budget ({self.config.max_tokens}) spent")
                self._publish(
                    run,
                    "error",
                    agent=step.agent,
                    message=f"Token budget of the investigation spent; {step.agent} skipped.",
                    recoverable=True,
                )
                return None
            step.status = StepStatus.RUNNING
            step.started_at = utcnow()
            sink = CallbackEventSink(lambda event: self._on_agent_event(run, step, event))
            started = time.perf_counter()
            try:
                agent = self.registry.get(step.agent)(
                    self._deps(step.agent, task.context.service, sink)
                )
                async with asyncio.timeout(self._step_timeout(step.agent)):
                    result = await agent.run(task)
            except TimeoutError:
                result = self._failed_result(
                    step, task, f"timed out after {self._step_timeout(step.agent):.0f}s", started
                )
            except asyncio.CancelledError:
                self._close_step(step, StepStatus.FAILED)
                raise
            except Exception as exc:  # never let one agent crash the investigation
                log.exception("Step %s (%s) crashed", step.id, step.agent)
                result = self._failed_result(step, task, f"{type(exc).__name__}: {exc}", started)

        result = shift_result(result, offset)
        inv.results.append(result)
        inv.usage = inv.usage + result.usage
        failed = result.status is AgentStatus.FAILED
        self._close_step(step, StepStatus.FAILED if failed else StepStatus.DONE)
        if result.status in (AgentStatus.FAILED, AgentStatus.PARTIAL):
            run.missing.append(f"{step.agent}: {result.error or result.summary}"[:300])
        for evidence in result.evidence:
            self._publish(
                run,
                "evidence_added",
                agent=step.agent,
                step_id=step.id,
                evidence=evidence.model_dump(mode="json"),
            )
        self._publish(
            run,
            "agent_finished",
            agent=step.agent,
            step_id=step.id,
            status=result.status.value,
            summary=result.summary,
            signals=result.signals,
            evidence_count=len(result.evidence),
            duration_ms=result.duration_ms,
            tokens=result.usage.total_tokens,
        )
        return result

    @staticmethod
    def _failed_result(
        step: InvestigationStep, task: AgentTask, reason: str, started: float
    ) -> AgentResult:
        return AgentResult(
            agent=step.agent,
            task_id=task.id,
            status=AgentStatus.FAILED,
            summary=f"{step.agent} failed: {reason}",
            error=reason,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    @staticmethod
    def _close_step(step: InvestigationStep, status: StepStatus) -> None:
        step.status = status
        step.finished_at = utcnow()

    def _on_agent_event(self, run: InvestigationRun, step: InvestigationStep, event: Event) -> None:
        """Agent events -> contract events (agent_finished is published with the result)."""
        if event.type == "agent_started":
            self._publish(
                run,
                "agent_started",
                agent=step.agent,
                step_id=step.id,
                objective=step.objective,
                round=step.round,
            )
        elif event.type == "tool_called":
            self._publish(
                run,
                "tool_called",
                agent=step.agent,
                step_id=step.id,
                tool=event.data.get("tool"),
                status=event.data.get("status"),
                duration_ms=event.data.get("duration_ms"),
            )

    # -- finishing ---------------------------------------------------------------------------

    def _finish(self, run: InvestigationRun) -> Investigation:
        inv = run.investigation
        order = {step.id: i for i, step in enumerate(inv.steps)}
        inv.results.sort(key=lambda r: order.get(r.task_id, len(order)))
        if inv.status is InvestigationStatus.RUNNING:
            inv.status = (
                InvestigationStatus.PARTIAL if run.missing else InvestigationStatus.COMPLETED
            )
            if run.missing and all(
                s.status in (StepStatus.FAILED, StepStatus.SKIPPED)
                for s in inv.steps
                if s.round == 1
            ):
                inv.status = InvestigationStatus.FAILED
        inv.completed_at = utcnow()
        inv.duration_ms = round((time.perf_counter() - run.started) * 1000, 2)
        inv.versions = self._versions(inv)
        self._publish(
            run, "investigation_finished", status=inv.status.value, duration_ms=inv.duration_ms
        )
        return inv

    def _versions(self, inv: Investigation) -> dict[str, str]:
        models = sorted({r.model for r in inv.results if r.model})
        prompts = sorted({r.prompt_version for r in inv.results if r.prompt_version})
        return {
            "orchestrator": "1",
            "profile": self.settings.profile,
            "model": ",".join(models) or getattr(self.llm, "name", "none"),
            "prompts": ",".join(prompts),
        }

    def _publish(
        self, run: InvestigationRun, type_: Any, *, agent: str | None = None, **data: Any
    ) -> None:
        self.bus.publish(type_, run.id, agent=agent, **data)
