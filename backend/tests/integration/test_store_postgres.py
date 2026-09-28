"""Evidence store on the compose Postgres (127.0.0.1:15432) in a throwaway schema."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from uuid import uuid4

import pytest
import sqlalchemy as sa

from aiops.core.config import load_settings
from aiops.core.events import EventBus
from aiops.core.guardrails.approvals import ActionProposal, ActionStatus
from aiops.orchestrator.engine import InvestigationRequest, Orchestrator
from aiops.orchestrator.replay import load_replay
from aiops.store.db import StoreError, default_database_url, sync_url
from aiops.store.repository import InvestigationStore
from aiops.store.sync_stores import SqlApprovalStore

pytestmark = pytest.mark.integration


@pytest.fixture
def schema() -> Iterator[str]:
    name = f"investigations_test_{uuid4().hex[:8]}"
    yield name
    engine = sa.create_engine(sync_url(default_database_url()))
    with engine.begin() as conn:
        conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
    engine.dispose()


def test_investigation_survives_restart_and_replays_events(schema: str) -> None:
    settings = load_settings("local")
    bus = EventBus()
    orch = Orchestrator(settings, replay=load_replay(settings, "S1"), bus=bus)
    inv = asyncio.run(orch.investigate(InvestigationRequest(question="", mode="replay")))
    try:
        InvestigationStore(default_database_url(), schema).migrate()
    except StoreError as exc:
        pytest.skip(f"compose Postgres not reachable: {exc}")

    async def write() -> None:
        store = InvestigationStore(default_database_url(), schema)
        await store.save(inv)
        await store.save_events(bus.history(inv.id))
        await store.close()

    async def read() -> None:  # a fresh store = a restarted process
        store = InvestigationStore(default_database_url(), schema)
        assert await store.get(inv.id) == inv
        assert len(await store.events(inv.id)) == len(bus.history(inv.id))
        assert [i.id for i in await store.search(service="payment-service")] == [inv.id]
        await store.close()

    asyncio.run(write())
    asyncio.run(read())

    approvals = SqlApprovalStore(default_database_url(), schema)
    proposal = ActionProposal(
        action="create_ticket",
        capability="tickets",
        tool="jira_create_issue",
        reason="test",
        requested_by="test",
        investigation_id=inv.id,
    )
    approvals.save(proposal)
    assert [p.id for p in approvals.list(ActionStatus.PENDING)] == [proposal.id]
