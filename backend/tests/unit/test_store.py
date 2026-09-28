"""Evidence store (PR-032) on SQLite: migrations, round trip, filters, event replay,
approvals + audit behind the existing interfaces (and the JSONL fallback)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from aiops.core.config import Settings, StorageConfig, load_settings
from aiops.core.events import EventBus
from aiops.core.guardrails.approvals import (
    ActionProposal,
    ActionStatus,
    ApprovalAuditRecord,
    ApprovalService,
)
from aiops.core.guardrails.audit import AuditRecord, MemoryAuditSink
from aiops.core.models import Investigation, ToolCall
from aiops.mcp.registry import MCPRegistry
from aiops.orchestrator.engine import InvestigationRequest, Orchestrator
from aiops.orchestrator.replay import load_replay
from aiops.store import tables
from aiops.store.db import StoreError, sync_engine, upgrade
from aiops.store.repository import EXCERPT_CHARS, InvestigationStore, summary_of
from aiops.store.sync_stores import SqlApprovalAuditSink, SqlApprovalStore, SqlAuditSink

CONFIG = Path(__file__).resolve().parents[3] / "config"


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings("local", CONFIG)


@pytest.fixture(scope="module")
def replayed(settings: Settings) -> tuple[Investigation, EventBus]:
    bus = EventBus()
    orch = Orchestrator(settings, replay=load_replay(settings, "S1"), bus=bus)
    inv = asyncio.run(orch.investigate(InvestigationRequest(question="", mode="replay")))
    return inv, bus


def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'store.db'}"


def test_migrations_are_idempotent_and_create_every_table(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path)
    upgrade(url, "investigations")
    upgrade(url, "investigations")  # no-op the second time
    names = set(sa.inspect(sync_engine(url, "investigations")).get_table_names())
    assert set(tables.metadata.tables) | {"alembic_version"} <= names


def test_round_trip_filters_and_event_replay(
    tmp_path: Path, replayed: tuple[Investigation, EventBus]
) -> None:
    inv, bus = replayed
    store = InvestigationStore(sqlite_url(tmp_path))
    store.migrate()

    async def scenario() -> None:
        await store.save(inv)
        await store.save(inv)  # replace, not duplicate
        await store.save_events(bus.history(inv.id))
        loaded = await store.get(inv.id)
        assert loaded == inv  # survives a "restart": the full document round-trips
        older = inv.model_copy(
            update={"id": "inv-older", "created_at": inv.created_at - timedelta(days=1)}
        )
        await store.save(older)
        assert [i.id for i in await store.search()] == [inv.id, "inv-older"]
        assert [i.id for i in await store.search(limit=1)] == [inv.id]
        assert [i.id for i in await store.search(before=inv.created_at)] == ["inv-older"]
        assert await store.search(service="order-service") == []
        assert len(await store.search(service="payment-service", mode="replay")) == 2
        assert len(await store.search(q="payment api")) == 2
        events = await store.events(inv.id)
        assert [e.seq for e in events] == [e.seq for e in bus.history(inv.id)]
        assert [e.type for e in events] == [e.type for e in bus.history(inv.id)]
        assert [e.seq for e in await store.events(inv.id, after_seq=100)] == [
            e.seq for e in bus.history(inv.id) if e.seq > 100
        ]
        assert await store.count() == 2
        assert await store.delete_mode("replay") == 2
        assert await store.get(inv.id) is None and await store.events(inv.id) == []
        await store.close()

    asyncio.run(scenario())


def test_normalized_rows_keep_excerpts_not_raw_logs(
    tmp_path: Path, replayed: tuple[Investigation, EventBus]
) -> None:
    inv, _ = replayed
    url = sqlite_url(tmp_path)
    store = InvestigationStore(url)
    store.migrate()

    async def save() -> None:
        await store.save(inv)
        await store.close()

    asyncio.run(save())
    with sync_engine(url, "investigations").connect() as conn:
        steps = conn.execute(sa.select(sa.func.count()).select_from(tables.step)).scalar()
        excerpts = [r[0] for r in conn.execute(sa.select(tables.evidence.c.excerpt)) if r[0]]
        calls = conn.execute(sa.select(sa.func.count()).select_from(tables.tool_call)).scalar()
    assert steps == len(inv.steps)
    assert calls == sum(len(r.tool_calls) for r in inv.results)
    assert excerpts and max(len(e) for e in excerpts) <= EXCERPT_CHARS
    summary = summary_of(inv)
    assert set(summary) == {
        "id",
        "incident",
        "status",
        "report",
        "affected_services",
        "created_at",
        "completed_at",
        "duration_ms",
        "mode",
    }


def test_sql_approval_store_behind_the_interface(tmp_path: Path, settings: Settings) -> None:
    url = sqlite_url(tmp_path)
    storage = StorageConfig(database_url=url, approvals="postgres")
    tuned = settings.model_copy(update={"storage": storage})
    service = ApprovalService.from_settings(tuned)
    assert isinstance(service.store, SqlApprovalStore)
    assert isinstance(service.audit, SqlApprovalAuditSink)
    proposal = ActionProposal(
        action="create_ticket",
        capability="tickets",
        tool="jira_create_issue",
        arguments={"project_key": "OPS", "summary": "x"},
        reason="RCA follow-up",
        requested_by="test",
        investigation_id="inv-1",
    )
    service.store.save(proposal)
    service.store.save(proposal.model_copy(update={"status": ActionStatus.APPROVED}))
    assert service.store.get(proposal.id) is not None
    assert [p.id for p in service.store.list(ActionStatus.APPROVED)] == [proposal.id]
    assert service.store.list(ActionStatus.PENDING) == []
    service.audit.record(
        ApprovalAuditRecord(
            proposal_id=proposal.id,
            action="create_ticket",
            capability="tickets",
            tool="jira_create_issue",
            arguments_sha256=proposal.arguments_sha256,
            from_status=None,
            to_status=ActionStatus.PENDING,
            actor="test",
        )
    )
    with sync_engine(url, "investigations").connect() as conn:
        links = conn.execute(sa.select(tables.approval_link)).all()
        audits = conn.execute(
            sa.select(sa.func.count()).select_from(tables.approval_audit)
        ).scalar()
    assert [(link.approval_id, link.investigation_id) for link in links] == [(proposal.id, "inv-1")]
    assert audits == 1


def record() -> AuditRecord:
    call = ToolCall(agent="logs", capability="logs", tool="execute_esql")
    return AuditRecord(request_id="req-1", investigation_id="inv-1", tool_call=call)


def test_sql_audit_sink_and_jsonl_fallback(tmp_path: Path, settings: Settings) -> None:
    url = sqlite_url(tmp_path)
    fallback = MemoryAuditSink()
    sink = SqlAuditSink(url, "investigations", fallback=fallback)
    sink.record(record())
    with sync_engine(url, "investigations").connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(tables.audit)).scalar() == 1
    assert fallback.records == []

    broken = SqlAuditSink("sqlite:////nonexistent-dir/x/store.db", "investigations", fallback)
    broken.record(record())
    assert len(fallback.records) == 1  # never lost

    tuned = settings.model_copy(
        update={"storage": StorageConfig(database_url=url, audit="postgres")}
    )
    assert isinstance(MCPRegistry(tuned).audit, SqlAuditSink)


def test_unreachable_store_is_a_readable_error() -> None:
    with pytest.raises(StoreError, match="unreachable"):
        upgrade("postgresql+psycopg://nobody:x@127.0.0.1:1/none", "investigations")
