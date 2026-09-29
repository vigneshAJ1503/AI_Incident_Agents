"""Orchestrator (PR-031): rounds, gap analysis, partial failure, timeouts, cancellation,
token budget and the contract's SSE event types. Zero tokens (replay + scripted agents)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

import aiops.agents  # noqa: F401
from aiops.agents.base import AgentDeps, AgentSpec, BaseAgent
from aiops.agents.registry import AgentRegistry
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import OrchestratorConfig, Settings, load_settings
from aiops.core.events import INVESTIGATION_EVENT_TYPES, EventBus, EventSink
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.core.models import (
    AgentResult,
    AgentStatus,
    AgentTask,
    Evidence,
    EvidenceKind,
    InvestigationStatus,
    StepStatus,
    TokenUsage,
)
from aiops.core.prompts import PromptLoader
from aiops.llm.fake import FakeLLMProvider
from aiops.mcp.registry import MCPRegistry
from aiops.orchestrator.engine import InvestigationRequest, Orchestrator
from aiops.orchestrator.gaps import analyze_gaps
from aiops.orchestrator.replay import load_replay, shift_text

CONFIG = Path(__file__).resolve().parents[3] / "config"
NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings("local", CONFIG)


# --------------------------------------------------------------------------- scripted agents


class Scripted(BaseAgent):
    """Test agent: behaviour per name, no MCP, no LLM."""

    behaviours: ClassVar[dict[str, str]] = {}
    seen: ClassVar[list[AgentTask]] = []

    async def run(self, task: AgentTask) -> AgentResult:
        Scripted.seen.append(task)
        self.emit("agent_started", task, objective=task.objective)
        self.emit("tool_called", task, tool="probe", status="ok", duration_ms=1.0)
        behaviour = self.behaviours.get(self.name, "ok")
        if behaviour == "crash":
            raise RuntimeError("boom")
        if behaviour == "hang":
            await asyncio.sleep(30)
        signals = {"logs": ["db_timeout_errors_up"], "metrics": ["latency_up"]}.get(self.name, [])
        evidence = Evidence(kind=EvidenceKind.LOG, source=f"{self.name}.probe", summary="x")
        return AgentResult(
            agent=self.name,
            task_id=task.id,
            status=AgentStatus.SUCCESS if signals else AgentStatus.NO_SIGNAL,
            summary=f"{self.name} done",
            evidence=[evidence],
            signals=signals,
            usage=TokenUsage(input_tokens=60, output_tokens=40, calls=1),
        )


def scripted(name: str, capability: str) -> type[BaseAgent]:
    spec = AgentSpec(
        name=name,
        version="1",
        description=f"{name} test agent",
        capabilities=[capability],
        evidence_kind=EvidenceKind.LOG,
        prompt=name,
    )
    return type(f"Scripted_{name}", (Scripted,), {"spec": spec})


def registry() -> AgentRegistry:
    reg = AgentRegistry()
    for name, cap in [
        ("logs", "logs"),
        ("metrics", "metrics"),
        ("alerts", "alerts"),
        ("k8s", "k8s"),
        ("code", "code"),
        ("knowledge", "knowledge"),
        ("tickets", "tickets"),
    ]:
        reg.register(scripted(name, cap))
    return reg


def orchestrator(settings: Settings, **config: object) -> tuple[Orchestrator, EventBus]:
    tuned = settings.model_copy(update={"orchestrator": OrchestratorConfig(**config)})  # type: ignore[arg-type]

    def deps(agent: str, service: str | None, events: EventSink) -> AgentDeps:
        return AgentDeps(
            settings=tuned,
            llm=FakeLLMProvider(),
            mcp=MCPRegistry(tuned, audit=MemoryAuditSink()),
            prompts=PromptLoader(CONFIG / "prompts"),
            catalog=ServiceCatalog.from_settings(tuned),
            events=events,
        )

    bus = EventBus()
    orch = Orchestrator(
        tuned, llm=FakeLLMProvider(), bus=bus, registry=registry(), deps_factory=deps
    )
    return orch, bus


def request(
    question: str = "Payment API is returning HTTP 500 in production",
) -> InvestigationRequest:
    return InvestigationRequest(question=question, end=NOW)


@pytest.fixture(autouse=True)
def _reset() -> None:
    Scripted.behaviours = {}
    Scripted.seen = []


# --------------------------------------------------------------------------- executor


def test_two_rounds_hints_symptoms_and_events(settings: Settings) -> None:
    orch, bus = orchestrator(settings)
    inv = asyncio.run(orch.investigate(request()))
    assert inv.status is InvestigationStatus.COMPLETED
    assert [s.round for s in inv.steps] == [1, 1, 1, 1, 1, 2, 2]
    assert all(s.status is StepStatus.DONE for s in inv.steps)
    assert inv.usage.total_tokens == 7 * 100 and inv.duration_ms is not None
    # task id == step id; round 2 got the round-1 findings
    tasks = {t.agent: t for t in Scripted.seen}
    assert {t.id for t in Scripted.seen} == {s.id for s in inv.steps}
    assert tasks["knowledge"].hints["signals"] == ["db_timeout_errors_up", "latency_up"]
    assert tasks["knowledge"].hints["keywords"] == ["connection pool"]
    assert tasks["tickets"].context.symptoms == ["db_timeout_errors_up", "latency_up"]
    assert tasks["logs"].context.symptoms == ["http_5xx"]
    assert inv.context is not None
    assert inv.context.symptoms == ["http_5xx", "db_timeout_errors_up", "latency_up"]

    events = bus.history(inv.id)
    assert {e.type for e in events} <= set(INVESTIGATION_EVENT_TYPES)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    types = [e.type for e in events]
    assert types[0] == "investigation_started" and types[-1] == "investigation_finished"
    assert types.index("plan_created") < types.index("round_started")
    assert types.count("round_started") == 2
    assert types.count("agent_started") == types.count("agent_finished") == 7
    assert types.count("evidence_added") == 7 and types.count("tool_called") == 7
    finished = [e for e in events if e.type == "agent_finished"]
    assert set(finished[0].data) == {
        "step_id",
        "status",
        "summary",
        "signals",
        "evidence_count",
        "duration_ms",
        "tokens",
        "cost_usd",  # additive (PR-041)
    }
    started = next(e for e in events if e.type == "agent_started")
    assert set(started.data) == {"step_id", "objective", "round"}


def test_one_crashing_agent_makes_it_partial_not_a_crash(settings: Settings) -> None:
    Scripted.behaviours = {"metrics": "crash"}
    orch, _ = orchestrator(settings)
    inv = asyncio.run(orch.investigate(request()))
    assert inv.status is InvestigationStatus.PARTIAL
    metrics = next(r for r in inv.results if r.agent == "metrics")
    assert metrics.status is AgentStatus.FAILED and "boom" in (metrics.error or "")
    assert sum(r.status is AgentStatus.SUCCESS for r in inv.results) >= 1
    # The others all ran (round 2 included), and gap analysis retried the failed metrics
    # agent for the round-1 db_timeout signal (it failed again).
    assert len(inv.results) == 8
    assert [s.round for s in inv.steps if s.agent == "metrics"] == [1, 2]


def test_step_timeout(settings: Settings) -> None:
    Scripted.behaviours = {"code": "hang"}
    orch, _ = orchestrator(settings, step_timeout_s=0.2)
    inv = asyncio.run(orch.investigate(request()))
    code = next(r for r in inv.results if r.agent == "code")
    assert code.status is AgentStatus.FAILED and "timed out" in code.summary
    assert inv.status is InvestigationStatus.PARTIAL


def test_concurrency_limit(settings: Settings) -> None:
    orch, bus = orchestrator(settings, max_concurrency=1)
    inv = asyncio.run(orch.investigate(request()))
    types = [e.type for e in bus.history(inv.id) if e.type in ("agent_started", "agent_finished")]
    assert types == ["agent_started", "agent_finished"] * 7  # strictly one at a time


def test_token_budget_skips_remaining_steps(settings: Settings) -> None:
    orch, bus = orchestrator(settings, max_tokens=250, max_concurrency=1)
    inv = asyncio.run(orch.investigate(request()))
    assert inv.status is InvestigationStatus.PARTIAL
    assert sum(s.status is StepStatus.SKIPPED for s in inv.steps) == 4
    assert any(e.type == "error" and e.data["recoverable"] for e in bus.history(inv.id))


def test_cancellation(settings: Settings) -> None:
    Scripted.behaviours = {"logs": "hang"}
    orch, bus = orchestrator(settings)

    async def scenario() -> InvestigationStatus:
        task = asyncio.create_task(orch.investigate(request(), investigation_id="inv-cancel"))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if any(e.type == "agent_started" for e in bus.history("inv-cancel")):
                break
        assert orch.cancel("inv-cancel")
        return (await task).status

    assert asyncio.run(scenario()) is InvestigationStatus.CANCELLED
    assert bus.history("inv-cancel")[-1].type == "investigation_finished"


def test_clarification_stops_before_agents(settings: Settings) -> None:
    orch, bus = orchestrator(settings)
    inv = asyncio.run(orch.investigate(request("Something is broken")))
    assert inv.status is InvestigationStatus.NEEDS_CLARIFICATION
    assert inv.clarification_question and inv.clarification_candidates
    assert Scripted.seen == []
    types = [e.type for e in bus.history(inv.id)]
    assert types == ["investigation_started", "clarification_needed", "investigation_finished"]


# --------------------------------------------------------------------------- gap analysis


def result(agent: str, signals: list[str], summary: str = "") -> AgentResult:
    return AgentResult(
        agent=agent,
        task_id=f"t-{agent}",
        status=AgentStatus.SUCCESS,
        summary=summary,
        signals=signals,
    )


def test_gap_analysis_maps_signals_to_followups(settings: Settings) -> None:
    catalog = ServiceCatalog.from_settings(settings)
    enabled = {"logs", "metrics", "code", "k8s", "knowledge", "tickets", "alerts"}
    gaps = analyze_gaps(
        [
            result(
                "logs",
                ["dependency_timeouts", "error_rate_up"],
                "Timeout calling inventory-service",
            ),
            result("metrics", ["no_anomaly"]),
        ],
        service="order-service",
        catalog=catalog,
        enabled_agents=enabled,
        round2_agents={"knowledge", "tickets"},
    )
    assert gaps.signals == ["dependency_timeouts", "error_rate_up"]  # benign dropped
    assert [(f.agent, f.service) for f in gaps.followups] == [
        ("logs", "inventory-service"),
        ("metrics", "inventory-service"),
    ]
    assert gaps.keywords == ["dependency timeout"]

    # Incident-service follow-ups are deduplicated when round 1 already covered them...
    gaps = analyze_gaps(
        [result("logs", ["db_timeout_errors_up"]), result("code", []), result("metrics", [])],
        service="payment-service",
        catalog=catalog,
        enabled_agents=enabled,
        round2_agents={"knowledge", "tickets"},
    )
    assert gaps.followups == [] and len(gaps.skipped) == 2
    # ... and re-run when that agent failed in round 1.
    failed = result("code", []).model_copy(update={"status": AgentStatus.FAILED})
    gaps = analyze_gaps(
        [result("logs", ["db_timeout_errors_up"]), failed],
        service="payment-service",
        catalog=catalog,
        enabled_agents=enabled,
        round2_agents={"knowledge", "tickets"},
    )
    assert [(f.agent, f.reason) for f in gaps.followups] == [
        ("code", "db_timeout_errors_up"),
        ("metrics", "db_timeout_errors_up"),
    ]


# --------------------------------------------------------------------------- replay


@pytest.mark.parametrize("scenario", ["S0", "S1", "S2", "S3", "S4", "S5"])
def test_replay_investigations_complete_with_zero_tokens(settings: Settings, scenario: str) -> None:
    bus = EventBus()
    orch = Orchestrator(settings, replay=load_replay(settings, scenario), bus=bus)
    inv = asyncio.run(orch.investigate(InvestigationRequest(question="", mode="replay")))
    assert inv.status is InvestigationStatus.COMPLETED, [r.error for r in inv.results]
    assert inv.mode == "replay" and inv.usage.total_tokens == 0
    assert {r.agent for r in inv.results} == {
        "logs",
        "metrics",
        "alerts",
        "k8s",
        "code",
        "knowledge",
        "tickets",
    }
    assert all(r.status is not AgentStatus.FAILED for r in inv.results)
    assert {e.type for e in bus.history(inv.id)} <= set(INVESTIGATION_EVENT_TYPES)
    # Live-recorded fixtures (k8s, metrics) are normalized onto the scenario clock.
    stamps = [e.timestamp for e in inv.evidence if e.timestamp is not None]
    assert all(NOW - timedelta(days=120) < ts <= NOW + timedelta(minutes=1) for ts in stamps)


def test_shift_text_keeps_formats() -> None:
    delta = timedelta(hours=1, minutes=5)
    text = "since 2026-09-26T09:33:00Z; at 2026-09-25 09:55Z; t=2026-09-25T10:10:07.571Z"
    assert shift_text(text, delta) == (
        "since 2026-09-26T10:38:00Z; at 2026-09-25 11:00Z; t=2026-09-25T11:15:07.571Z"
    )
