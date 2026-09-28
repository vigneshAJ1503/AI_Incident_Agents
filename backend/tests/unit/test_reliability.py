"""Reliability (PR-042): circuit breakers, one data source down -> PARTIAL with the missing
source named, graceful shutdown, and the stuck-investigation reaper. Zero tokens."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from aiops.agents.base import AgentDeps, AgentSpec, BaseAgent
from aiops.agents.registry import AgentRegistry
from aiops.api.runner import InvestigationRunner
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import CapabilityLimits, OrchestratorConfig, Settings, load_settings
from aiops.core.events import EventBus, EventSink, InvestigationEvent
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.core.models import (
    AgentStatus,
    EvidenceKind,
    Incident,
    Investigation,
    InvestigationStatus,
)
from aiops.llm.fake import FakeLLMProvider
from aiops.mcp.breaker import BREAKERS, CircuitBreaker, CircuitOpenError
from aiops.mcp.registry import MCPRegistry
from aiops.orchestrator.engine import InvestigationRequest, Orchestrator
from aiops.store.repository import InvestigationStore
from tests.security.leaky import LeakyLogsAgent, leaky_server
from tests.unit.agent_helpers import make_prompts, search_then_submit

CONFIG = Path(__file__).resolve().parents[3] / "config"
DOWN = "http://127.0.0.1:9/mcp"  # the discard port: connection refused at once


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings("local", CONFIG)


# --------------------------------------------------------------------------- the breaker


def test_breaker_opens_half_opens_and_closes() -> None:
    now = [0.0]
    breaker = CircuitBreaker("logs", failure_threshold=2, reset_s=10, clock=lambda: now[0])
    assert breaker.allow() and breaker.state == "closed"
    breaker.record_failure("timeout")
    assert breaker.state == "closed"
    breaker.record_failure("timeout")
    assert breaker.state == "open" and not breaker.allow()
    with pytest.raises(CircuitOpenError, match="circuit open for 'logs' after 2"):
        breaker.check()
    now[0] = 10.0
    assert breaker.allow() and breaker.state == "half_open"  # the one probe
    assert not breaker.allow()  # nobody else while it runs
    breaker.record_failure("refused")
    assert breaker.state == "open" and not breaker.allow()  # probe failed: open again
    now[0] = 21.0
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state == "closed" and breaker.failures == 0 and breaker.allow()


# --------------------------------------------------------------------------- one source down


class MetricsProbe(BaseAgent):
    spec = AgentSpec(
        name="metrics",
        version="1",
        description="LLM-loop agent bound to the metrics capability",
        capabilities=["metrics"],
        evidence_kind=EvidenceKind.METRIC,
        prompt="echo",
    )


def tuned(settings: Settings) -> Settings:
    caps = dict(settings.capabilities)
    caps["metrics"] = caps["metrics"].model_copy(
        update={"limits": CapabilityLimits(breaker_failures=1, breaker_reset_s=60)}
    )
    return settings.model_copy(
        update={
            "capabilities": caps,
            "orchestrator": OrchestratorConfig(
                max_rounds=1, llm_rca=False, llm_planner_fallback=False
            ),
        }
    )


def orchestrator(settings: Settings, tmp_path: Path) -> Orchestrator:
    registry = AgentRegistry()
    registry.register(LeakyLogsAgent)
    registry.register(MetricsProbe)
    llm = FakeLLMProvider(responder=search_then_submit)
    prompts = make_prompts(tmp_path / f"prompts-{uuid4().hex[:6]}")

    def deps(agent: str, service: str | None, events: EventSink) -> AgentDeps:
        return AgentDeps(
            settings=settings,
            llm=llm,
            mcp=MCPRegistry(
                settings,
                audit=MemoryAuditSink(),
                overrides={"logs": leaky_server(), "metrics": DOWN},
            ),
            prompts=prompts,
            catalog=ServiceCatalog.from_settings(settings),
            events=events,
        )

    return Orchestrator(settings, llm=llm, registry=registry, deps_factory=deps)


def test_one_source_down_is_partial_and_named_then_short_circuited(
    settings: Settings, tmp_path: Path
) -> None:
    config = tuned(settings)
    request = InvestigationRequest(question="payment-service is returning 500 errors")

    first = asyncio.run(orchestrator(config, tmp_path).investigate(request))
    assert first.status is InvestigationStatus.PARTIAL
    by_agent = {r.agent: r for r in first.results}
    assert by_agent["logs"].status is AgentStatus.SUCCESS  # the healthy source still ran
    assert by_agent["metrics"].status is AgentStatus.FAILED
    assert "Data source unavailable" in (by_agent["metrics"].error or "")
    assert first.report is not None
    gaps = [q for q in first.report.open_questions if q.startswith("Not checked")]
    assert any("metrics" in q and "unavailable" in q for q in gaps), gaps
    assert "metrics" in first.report.markdown

    # The circuit is now open: the next investigation doesn't wait on the dead server.
    assert "open" in BREAKERS.states().values()
    started = time.perf_counter()
    second = asyncio.run(orchestrator(config, tmp_path).investigate(request))
    metrics = next(r for r in second.results if r.agent == "metrics")
    assert second.status is InvestigationStatus.PARTIAL
    assert "circuit open for 'metrics'" in (metrics.error or "")
    assert metrics.duration_ms < 250
    assert time.perf_counter() - started < 5
    assert second.report is not None
    assert any("circuit open" in q for q in second.report.open_questions)


async def test_toolset_short_circuits_while_open(settings: Settings) -> None:
    registry = MCPRegistry(settings, audit=MemoryAuditSink(), overrides={"logs": leaky_server()})
    async with registry.toolset("logs", agent="t") as toolset:
        breaker = registry.breaker("logs")
        assert breaker is not None
        ok = await toolset.call("search_logs", {"service": "payment-service"})
        assert ok.ok and breaker.state == "closed"
        for _ in range(breaker.failure_threshold):
            breaker.record_failure("timeout")
        skipped = await toolset.call("search_logs", {"service": "payment-service"})
    assert not skipped.ok and "circuit open" in (skipped.tool_call.error or "")
    with pytest.raises(CircuitOpenError):
        async with registry.toolset("logs", agent="t"):
            pass


def test_replays_never_use_a_breaker(settings: Settings, tmp_path: Path) -> None:
    assert MCPRegistry(settings, replay_dir=tmp_path).breaker("logs") is None


# --------------------------------------------------------------------------- lifecycle


@pytest.fixture
def store(tmp_path: Path) -> InvestigationStore:
    store = InvestigationStore(f"sqlite:///{tmp_path / 'r.db'}")
    store.migrate()
    return store


class Hanging:
    """An orchestrator that runs until cancelled."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.release = asyncio.Event()

    def running(self, investigation_id: str) -> Investigation | None:
        return None

    def cancel(self, investigation_id: str) -> bool:
        self.release.set()
        return True

    async def investigate(self, request: Any, *, investigation_id: str) -> Investigation:
        self.bus.publish("investigation_started", investigation_id, question=request.question)
        await self.release.wait()
        self.bus.publish("investigation_finished", investigation_id, status="cancelled")
        return Investigation(
            id=investigation_id,
            incident=Incident(title=request.question),
            status=InvestigationStatus.CANCELLED,
        )


async def test_graceful_shutdown_saves_cancelled_runs_with_a_reason(
    settings: Settings, store: InvestigationStore
) -> None:
    bus = EventBus()
    runner = InvestigationRunner(
        settings,
        store,
        bus,
        orchestrator_factory=lambda s, b, sc: Hanging(b),  # type: ignore[arg-type,return-value]
    )
    handle = await runner.start(InvestigationRequest(question="q"))
    await asyncio.sleep(0.05)
    await runner.shutdown(grace_s=5)
    stored = await store.get(handle.id)
    events = await store.events(handle.id)
    assert stored is not None and stored.status is InvestigationStatus.CANCELLED
    assert any("shutting down" in str(e.data.get("message")) for e in events)
    with pytest.raises(Exception, match="shutting down"):
        await runner.start(InvestigationRequest(question="late"))


async def test_reaper_fails_stuck_investigations_only(
    settings: Settings, store: InvestigationStore
) -> None:
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    old = now - timedelta(hours=1)

    def inv(inv_id: str, status: InvestigationStatus, created: datetime) -> Investigation:
        return Investigation(
            id=inv_id, incident=Incident(title=inv_id), status=status, created_at=created
        )

    await store.save(inv("inv-stuck", InvestigationStatus.RUNNING, old))
    await store.save_events(
        [
            InvestigationEvent(
                type="investigation_started", investigation_id="inv-stuck", timestamp=old, seq=1
            )
        ]
    )
    await store.save(inv("inv-orphan", InvestigationStatus.PENDING, old))  # no events at all
    await store.save(inv("inv-busy", InvestigationStatus.RUNNING, now - timedelta(minutes=2)))
    await store.save(inv("inv-done", InvestigationStatus.COMPLETED, old))

    runner = InvestigationRunner(settings, store, EventBus())
    reaped = await runner.reap_stuck(now=now)
    assert sorted(reaped) == ["inv-orphan", "inv-stuck"]
    stuck = await store.get("inv-stuck")
    assert stuck is not None and stuck.status is InvestigationStatus.FAILED
    events = await store.events("inv-stuck")
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[1].type == "error" and "no progress for 60 min" in events[1].data["message"]
    assert events[2].type == "investigation_finished" and events[2].data["status"] == "failed"
    busy = await store.get("inv-busy")
    assert busy is not None and busy.status is InvestigationStatus.RUNNING
    assert await runner.reap_stuck(now=now) == []  # idempotent

    off = settings.model_copy(
        update={"api": settings.api.model_copy(update={"stuck_after_s": 0.0})}
    )
    assert await InvestigationRunner(off, store, EventBus()).reap_stuck(now=now) == []
