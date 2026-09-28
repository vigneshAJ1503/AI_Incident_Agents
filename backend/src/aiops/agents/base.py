"""BaseAgent: a bounded, evidence-first tool-use loop.

Contract: ``AgentTask`` in -> ``AgentResult`` out. Never raises for expected failures.

Evidence-first by construction:
  * every successful tool call automatically becomes an ``Evidence`` item with an id;
  * the LLM finishes by calling ``submit`` with typed findings that cite those ids;
  * a report citing unknown ids, or a FACT without evidence, is rejected and retried.

Bounded: max_steps (LLM turns), max_tool_calls, max_execution_s and max_tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aiops.core.catalog import ServiceCatalog
from aiops.core.config import AgentLimits, Settings
from aiops.core.events import Event, EventSink, EventType, NullEventSink
from aiops.core.models import (
    AgentResult,
    AgentStatus,
    AgentTask,
    ClaimKind,
    Evidence,
    EvidenceKind,
    Finding,
    TokenUsage,
    ToolCall,
)
from aiops.core.prompts import Prompt, PromptLoader
from aiops.llm.base import ChatMessage, LLMError, LLMProvider, LLMToolCallError, ToolSpec
from aiops.llm.structured import SUBMIT_TOOL, submit_tool
from aiops.mcp.client import MCPClientError
from aiops.mcp.registry import MCPRegistry
from aiops.mcp.toolset import ToolOutcome, Toolset, truncate
from aiops.providers import PROVIDER_REGISTRY, Provider

log = logging.getLogger(__name__)

EVIDENCE_DATA_CHARS = 4_000


class AgentSpec(BaseModel):
    """Registry entry: what an agent is, what it can do and which tools it may use."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    description: str
    capabilities: list[str]  # MCP capabilities the agent needs, e.g. ["logs"]
    evidence_kind: EvidenceKind
    prompt: str  # config/prompts/<prompt>/vN.md


class ReportFinding(BaseModel):
    kind: ClaimKind
    type: str = Field(description="Short category, e.g. error_pattern, latency_spike, no_errors.")
    description: str
    evidence_ids: list[str] = Field(
        default_factory=list, description="Ids from 'evidence_id:' lines of tool results."
    )
    confidence: float | None = Field(default=None, ge=0, le=1)


class AgentReport(BaseModel):
    """What the LLM submits at the end of its investigation."""

    status: Literal["success", "no_signal"] = Field(
        description="success = found something relevant; no_signal = checked, nothing abnormal."
    )
    summary: str = Field(description="2-4 sentences, evidence-based.")
    findings: list[ReportFinding] = Field(default_factory=list)
    signals: list[str] = Field(
        default_factory=list,
        description="Machine-usable snake_case facts, e.g. db_timeout_errors_up.",
    )
    confidence: float = Field(ge=0, le=1)
    suggested_followups: list[str] = Field(default_factory=list)


class ToolLimitExceededError(Exception):
    pass


@dataclass
class AgentDeps:
    settings: Settings
    llm: LLMProvider
    mcp: MCPRegistry
    prompts: PromptLoader
    catalog: ServiceCatalog
    events: EventSink = field(default_factory=NullEventSink)


class AgentRun:
    """Mutable state of one agent execution."""

    def __init__(self, agent: BaseAgent, task: AgentTask, limits: AgentLimits) -> None:
        self.agent = agent
        self.task = task
        self.limits = limits
        self.toolsets: dict[str, Toolset] = {}
        self.evidence: list[Evidence] = []
        self.tool_calls: list[ToolCall] = []
        self.usage = TokenUsage()
        self.model: str | None = None
        self.prompt_ref: str | None = None
        self._tool_index: dict[str, Toolset] = {}

    async def attach(self, toolsets: dict[str, Toolset]) -> list[ToolSpec]:
        self.toolsets = toolsets
        specs: list[ToolSpec] = []
        for toolset in toolsets.values():
            for spec in await toolset.specs():
                if spec.name in self._tool_index:
                    raise ValueError(f"Tool name collision across capabilities: {spec.name}")
                self._tool_index[spec.name] = toolset
                specs.append(spec)
        return specs

    def toolset(self, capability: str) -> Toolset:
        return self.toolsets[capability]

    async def call_tool(
        self, tool: str, arguments: dict[str, Any], *, summary: str | None = None
    ) -> tuple[ToolOutcome, Evidence | None]:
        """Call a tool with limits + events; successful calls become evidence."""
        if len(self.tool_calls) >= self.limits.max_tool_calls:
            raise ToolLimitExceededError(f"max_tool_calls={self.limits.max_tool_calls} reached")
        toolset = self._tool_index.get(tool) or next(iter(self.toolsets.values()))
        outcome = await toolset.call(tool, arguments)  # unknown tools come back "blocked"
        self.tool_calls.append(outcome.tool_call)
        self.agent.emit(
            "tool_called",
            self.task,
            tool=tool,
            status=outcome.tool_call.status,
            duration_ms=outcome.tool_call.duration_ms,
        )
        if not outcome.ok:
            return outcome, None
        evidence = Evidence(
            kind=self.agent.spec.evidence_kind,
            source=f"{toolset.capability}.{tool}",
            summary=summary or f"{tool} result",
            query=json.dumps(arguments, sort_keys=True, default=str),
            data=_evidence_data(outcome),
        )
        self.evidence.append(evidence)
        return outcome, evidence

    def add_evidence(self, evidence: Evidence) -> Evidence:
        self.evidence.append(evidence)
        return evidence

    # -- results ------------------------------------------------------------------------

    def result_from_report(self, report: AgentReport) -> AgentResult:
        """Validate citations and build the AgentResult. Raises ValueError to reject."""
        known = {e.id for e in self.evidence}
        unknown = sorted({i for f in report.findings for i in f.evidence_ids} - known)
        if unknown:
            raise ValueError(
                f"findings cite unknown evidence ids {unknown}; valid ids: {sorted(known)}"
            )
        findings = [Finding(**f.model_dump()) for f in report.findings]
        return self._result(
            status=AgentStatus(report.status),
            summary=report.summary,
            findings=findings,
            signals=report.signals,
            confidence=report.confidence,
            suggested_followups=report.suggested_followups,
        )

    def partial(self, reason: str) -> AgentResult:
        status = AgentStatus.PARTIAL if self.evidence else AgentStatus.FAILED
        return self._result(status=status, summary=reason, error=reason)

    def failed(self, reason: str) -> AgentResult:
        return self._result(status=AgentStatus.FAILED, summary=reason, error=reason)

    def _result(self, **fields: Any) -> AgentResult:
        return AgentResult(
            agent=self.agent.spec.name,
            agent_version=self.agent.spec.version,
            task_id=self.task.id,
            evidence=list(self.evidence),
            **fields,
        )


def _evidence_data(outcome: ToolOutcome) -> dict[str, Any]:
    if outcome.data is not None:
        encoded = json.dumps(outcome.data, default=str)
        if len(encoded) <= EVIDENCE_DATA_CHARS:
            return {"result": outcome.data}
        return {"excerpt": truncate(encoded, EVIDENCE_DATA_CHARS)}
    return {"excerpt": truncate(outcome.text, EVIDENCE_DATA_CHARS)}


class BaseAgent(ABC):
    spec: ClassVar[AgentSpec]

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    @property
    def name(self) -> str:
        return self.spec.name

    def emit(self, type_: EventType, task: AgentTask, **data: Any) -> None:
        self.deps.events.emit(
            Event(
                type=type_,
                agent=self.name,
                task_id=task.id,
                investigation_id=task.investigation_id,
                data=data,
            )
        )

    # -- lifecycle ----------------------------------------------------------------------

    async def run(self, task: AgentTask) -> AgentResult:
        settings = self.deps.settings
        limits = settings.agent_limits(self.name)
        run = AgentRun(self, task, limits)
        started = time.perf_counter()
        self.emit("agent_started", task, objective=task.objective)
        try:
            async with AsyncExitStack() as stack:
                toolsets = {
                    cap: await stack.enter_async_context(
                        self.deps.mcp.toolset(
                            cap, agent=self.name, investigation_id=task.investigation_id
                        )
                    )
                    for cap in self.spec.capabilities
                }
                tool_specs = await run.attach(toolsets)
                async with asyncio.timeout(limits.max_execution_s):
                    result = await self.investigate(run, tool_specs)
        except TimeoutError:
            result = run.partial(f"Execution time limit ({limits.max_execution_s:.0f}s) reached.")
        except MCPClientError as exc:
            result = run.failed(f"Data source unavailable: {exc}")
        except LLMError as exc:
            result = run.partial(f"LLM error: {exc}")
        except Exception as exc:  # an agent must never crash the investigation
            log.exception("Agent %s crashed", self.name)
            result = run.failed(f"Agent error: {type(exc).__name__}: {exc}")

        result = result.model_copy(
            update={
                "tool_calls": run.tool_calls,
                "usage": run.usage,
                "model": run.model,
                "prompt_version": run.prompt_ref,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )
        self.emit(
            "agent_finished",
            task,
            status=result.status.value,
            summary=result.summary,
            evidence=len(result.evidence),
            tokens=result.usage.total_tokens,
        )
        return result

    async def investigate(self, run: AgentRun, tool_specs: list[ToolSpec]) -> AgentResult:
        """Default strategy: the LLM tool loop. Subclasses may add deterministic steps."""
        system, user = self.build_prompt(run)
        return await self.llm_loop(run, tool_specs, system, user)

    # -- prompting ----------------------------------------------------------------------

    def prompt_variables(self, run: AgentRun) -> dict[str, object]:
        ctx = run.task.context
        return {
            "question": ctx.question,
            "objective": run.task.objective,
            "service": ctx.service or "unknown",
            "environment": ctx.environment or "unknown",
            "start": ctx.time_range.start.isoformat(),
            "end": ctx.time_range.end.isoformat(),
            "symptoms": ", ".join(ctx.symptoms) or "none yet",
            "hints": json.dumps(run.task.hints, default=str) if run.task.hints else "none",
        }

    def provider[P: Provider](self, capability: str, base: type[P]) -> P:
        """The provider adapter the profile selects for ``capability`` (ADR-0012)."""
        return PROVIDER_REGISTRY.for_settings(self.deps.settings, capability, base)

    def prompt_fragments(self, prompt_text: str) -> list[Prompt]:
        """Prompt fragments of the selected providers (``config/prompts/providers/<cap>/
        <provider>/vN.md``), when the agent prompt asks for ``$provider_guidance``."""
        if "provider_guidance" not in prompt_text:
            return []
        fragments = []
        for capability in self.spec.capabilities:
            cap = self.deps.settings.capabilities.get(capability)
            cls = PROVIDER_REGISTRY.find(capability, cap.provider) if cap else None
            if cls is not None and cls.prompt_fragment:
                fragments.append(self.deps.prompts.load(cls.prompt_fragment))
        return fragments

    def build_prompt(self, run: AgentRun) -> tuple[str, str]:
        prompts = self.deps.prompts
        version = self.deps.settings.agent(self.name).prompt_version
        common = prompts.load("common")
        own = prompts.load(self.spec.prompt, version)
        variables = self.prompt_variables(run)
        fragments = self.prompt_fragments(own.text)
        # Vendor query guidance (ES|QL, JQL, ...) comes from the providers' prompt fragments.
        variables["provider_guidance"] = "\n\n".join(
            fragment.render(**variables).strip() for fragment in fragments
        )
        run.prompt_ref = "+".join([own.ref, *(fragment.ref for fragment in fragments)])
        system = f"{common.text}\n\n{own.render(**variables)}"
        user = (
            f"Objective: {run.task.objective}\n"
            f"Question from the engineer: {variables['question']}\n"
            f"Service: {variables['service']} | Environment: {variables['environment']}\n"
            f"Time range (UTC): {variables['start']} to {variables['end']}\n"
            f"Hints from earlier findings: {variables['hints']}\n\n"
            f"Investigate with the tools, then call '{SUBMIT_TOOL}' with your report. "
            "Cite evidence ids from the tool results."
        )
        return system, user

    # -- the loop -----------------------------------------------------------------------

    async def _deterministic_fallback(
        self, run: AgentRun, tool_specs: list[ToolSpec], system: str, user: str, exc: LLMError
    ) -> AgentResult:
        """Finish from the evidence already gathered, as in zero-LLM (replay) mode."""
        from aiops.evals.replay import echo_responder  # local: avoids an import cycle
        from aiops.llm.fake import FakeLLMProvider

        log.warning("Agent %s: LLM failed (%s); using the deterministic analysis", self.name, exc)
        self.emit("llm_called", run.task, step=0, tool_calls=0, fallback=type(exc).__name__)
        original = self.deps.llm
        self.deps.llm = FakeLLMProvider(responder=echo_responder())
        usage_before = run.usage
        try:
            result = await self.llm_loop(run, tool_specs, system, user)
        finally:
            self.deps.llm = original
            run.usage = usage_before  # the stand-in spends no real tokens
        note = f"LLM unavailable ({type(exc).__name__}): finished with the deterministic analysis."
        return result.model_copy(
            update={"suggested_followups": [*result.suggested_followups, note]}
        )

    async def llm_loop(
        self, run: AgentRun, tool_specs: list[ToolSpec], system: str, user: str
    ) -> AgentResult:
        submit = submit_tool(AgentReport, "Submit your final, evidence-cited report.")
        tools = [*tool_specs, submit]
        messages = [ChatMessage.system(system), ChatMessage.user(user)]
        role = self.deps.settings.agent(self.name).model_role

        for step in range(1, run.limits.max_steps + 1):
            if run.usage.total_tokens >= run.limits.max_tokens:
                return run.partial(f"Token budget ({run.limits.max_tokens}) reached.")
            # Last step (or no tool budget left): only `submit` is offered, so a real model
            # that keeps exploring still ends with an evidence-cited report.
            final = step == run.limits.max_steps or len(run.tool_calls) >= run.limits.max_tool_calls
            if final:
                messages.append(
                    ChatMessage.user(
                        f"Final step: no more investigation. Call '{SUBMIT_TOOL}' now with your "
                        "report, based only on the evidence ids you already have."
                    )
                )
            try:
                response = await self.deps.llm.generate(
                    messages, tools=[submit] if final else tools, tool_choice="required", role=role
                )
            except LLMToolCallError as exc:
                # The provider rejected the model's tool call (e.g. schema mismatch).
                # Tell the model exactly what was wrong and let it try again.
                self.emit("llm_called", run.task, step=step, tool_calls=0, rejected=True)
                messages.append(
                    ChatMessage.user(
                        f"Your previous tool call was rejected: {exc}. Call the tool again with "
                        f"arguments that match its JSON schema exactly (for '{SUBMIT_TOOL}': "
                        "every finding needs 'kind', 'type' and 'description')."
                    )
                )
                continue
            except LLMError as exc:
                # Rate limit / outage / unusable output: never let the LLM make the result
                # WORSE than no LLM. Finish with the deterministic analysis instead.
                return await self._deterministic_fallback(run, tool_specs, system, user, exc)
            run.usage = run.usage + response.usage
            run.model = response.model or run.model
            self.emit("llm_called", run.task, step=step, tool_calls=len(response.tool_calls))
            messages.append(response.as_message())

            if not response.tool_calls:
                messages.append(
                    ChatMessage.user(f"Call a tool, or call '{SUBMIT_TOOL}' to finish.")
                )
                continue

            for call in response.tool_calls:
                if call.name == SUBMIT_TOOL:
                    try:
                        if call.parse_error:
                            raise ValueError(f"arguments are not valid JSON: {call.parse_error}")
                        return run.result_from_report(AgentReport.model_validate(call.arguments))
                    except (ValidationError, ValueError) as exc:
                        messages.append(
                            ChatMessage.tool_result(
                                call.id, f"Rejected: {exc}. Fix the report and call submit again."
                            )
                        )
                    continue
                if call.parse_error:
                    messages.append(
                        ChatMessage.tool_result(
                            call.id, f"Invalid JSON arguments: {call.parse_error}"
                        )
                    )
                    continue
                try:
                    outcome, evidence = await run.call_tool(call.name, call.arguments)
                except ToolLimitExceededError as exc:
                    messages.append(
                        ChatMessage.tool_result(
                            call.id, f"{exc}. No more tool calls: call '{SUBMIT_TOOL}' now."
                        )
                    )
                    continue
                suffix = f"\nevidence_id: {evidence.id}" if evidence else ""
                messages.append(ChatMessage.tool_result(call.id, outcome.content + suffix))

        return run.partial(f"Step limit ({run.limits.max_steps}) reached before a report.")
