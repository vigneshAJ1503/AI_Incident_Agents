"""Shared helpers for the API tests (unit: SQLite; integration: compose Postgres)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx2

from aiops.api.app import create_app
from aiops.api.context import ApiContext, ExecutorFactory, build_context
from aiops.api.health import CapabilityHealth
from aiops.api.runner import OrchestratorFactory, default_orchestrator
from aiops.core.config import CapabilityConfig, Settings
from aiops.core.guardrails.approvals import (
    ActionProposal,
    ActionStatus,
    ApprovalError,
    ApprovalPolicy,
    ApprovalService,
    MemoryApprovalAuditSink,
    MemoryApprovalStore,
)
from aiops.faults import FaultError, FaultState
from aiops.store.repository import InvestigationStore


class FakeFaults:
    """In-memory fault controller (no kubectl, no cluster)."""

    def __init__(self) -> None:
        self.state: str | None = None
        self.calls: list[str] = []
        self.last_error: str | None = None
        self.in_progress = False  # tests flip it to simulate a background revert

    def reverting(self) -> bool:
        return self.in_progress

    def active(self) -> str | None:
        return self.state

    def inject(self, scenario_id: str) -> FaultState:
        if self.state:
            raise FaultError(f"Scenario {self.state} is already active")
        self.calls.append(f"inject {scenario_id}")
        self.state = scenario_id
        return FaultState(scenario=scenario_id, injected_at="2026-09-28T10:00:00Z")

    def revert(self) -> None:
        self.calls.append("revert")
        self.state = None


class FakeExecutor:
    """Stands in for ApprovalExecutor in unit tests: marks the proposal executed."""

    def __init__(self, service: ApprovalService) -> None:
        self.service = service

    async def execute(self, proposal_id: str, actor: str) -> ActionProposal:
        proposal = self.service.get(proposal_id)
        if proposal.status is not ActionStatus.APPROVED:
            raise ApprovalError(f"{proposal.id} is {proposal.status.value}")
        return self.service.mark_executed(
            proposal, actor, {"issue": {"key": "OPS-999", "url": "http://mock/browse/OPS-999"}}
        )


async def always_up(cap: CapabilityConfig, timeout: float) -> bool:
    return True


def memory_approvals(settings: Settings) -> ApprovalService:
    return ApprovalService(
        MemoryApprovalStore(),
        MemoryApprovalAuditSink(),
        ApprovalPolicy(settings),
        ttl=timedelta(hours=1),
    )


def make_context(
    settings: Settings,
    store: InvestigationStore,
    *,
    approvals: ApprovalService | None = None,
    orchestrator_factory: OrchestratorFactory = default_orchestrator,
    faults: FakeFaults | None = None,
    executor_factory: ExecutorFactory | None = None,
    reaper: bool = False,
) -> ApiContext:
    return build_context(
        settings,
        store=store,
        approvals=approvals or memory_approvals(settings),
        orchestrator_factory=orchestrator_factory,
        faults=faults or FakeFaults(),
        health=CapabilityHealth(settings, probe=always_up),
        executor_factory=executor_factory or FakeExecutor,  # type: ignore[arg-type]
        reaper=reaper,
    )


@asynccontextmanager
async def api_client(ctx: ApiContext) -> AsyncIterator[httpx2.AsyncClient]:
    """An httpx AsyncClient on the ASGI app (lifespan run by hand: ASGITransport skips it)."""
    app = create_app(ctx)
    await ctx.startup()
    transport = httpx2.ASGITransport(app=app)
    try:
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await ctx.shutdown()


def parse_sse(text: str) -> list[dict[str, Any]]:
    """SSE frames -> ``{"id", "event", "data"}`` dicts (data JSON-decoded)."""
    frames = []
    for block in text.strip().split("\n\n"):
        frame: dict[str, Any] = {}
        for line in block.splitlines():
            key, _, value = line.partition(": ")
            frame[key] = json.loads(value) if key == "data" else value
        if frame:
            frames.append(frame)
    return frames


async def wait_until_idle(ctx: ApiContext, timeout: float = 20.0) -> None:
    async with asyncio.timeout(timeout):
        while ctx.runner.running:
            await asyncio.sleep(0.02)
