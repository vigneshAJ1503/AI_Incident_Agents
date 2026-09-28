"""The API (PR-035) on the compose Postgres (127.0.0.1:15432) in a throwaway schema, served by
a real uvicorn server for the SSE test; approvals execute through the real ApprovalExecutor
against mock-tickets-mcp (make infra-up mcp-up). Zero tokens (replays)."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx2
import jsonschema
import pytest
import sqlalchemy as sa
import uvicorn

from aiops.api.app import create_app
from aiops.api.context import ApiContext, default_executor_factory
from aiops.core.config import Settings, StorageConfig, load_settings
from aiops.core.guardrails.approvals import ApprovalService
from aiops.core.models import Incident, Investigation, InvestigationReport, InvestigationStatus
from aiops.evals.investigation import score_investigation
from aiops.evals.scenario import load_scenarios
from aiops.orchestrator.dashboard import dashboard_summary
from aiops.seed.tickets import PostgresTicketSeeder, default_dsn
from aiops.store.db import StoreError, default_database_url, sync_url
from aiops.store.repository import InvestigationStore
from tests.api_support import api_client, make_context, parse_sse, wait_until_idle
from tests.conftest import REPO_ROOT
from tests.fixtures.scenario_context import FIXED_NOW

pytestmark = pytest.mark.integration


@pytest.fixture
def schema() -> Iterator[str]:
    name = f"api_test_{uuid4().hex[:8]}"
    yield name
    engine = sa.create_engine(sync_url(default_database_url()))
    with engine.begin() as conn:
        conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
    engine.dispose()


@pytest.fixture
def settings(schema: str) -> Settings:
    base = load_settings("local", REPO_ROOT / "config")
    storage = StorageConfig(db_schema=schema, approvals="postgres")
    return base.model_copy(update={"storage": storage})


@pytest.fixture
def store(settings: Settings) -> InvestigationStore:
    store = InvestigationStore.from_settings(settings)
    try:
        store.migrate()
    except StoreError as exc:
        pytest.skip(f"compose Postgres not reachable: {exc}")
    return store


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@asynccontextmanager
async def served(ctx: ApiContext) -> AsyncIterator[str]:
    """A real uvicorn server (real HTTP, real chunked SSE) on a free local port."""
    port = free_port()
    config = uvicorn.Config(
        create_app(ctx), host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def test_replay_s1_streams_live_and_the_report_matches_ground_truth(
    settings: Settings, store: InvestigationStore
) -> None:
    ctx = make_context(settings, store)
    scenario = next(s for s in load_scenarios(REPO_ROOT / "scenarios") if s.id == "S1")
    async with served(ctx) as base, httpx2.AsyncClient(base_url=base, timeout=30) as client:
        created = await client.post("/api/investigations", json={"question": scenario.question})
        assert created.status_code == 202, created.text
        inv_id = created.json()["id"]
        assert created.json()["scenario"] == "S1"
        chunks: list[str] = []
        async with client.stream("GET", f"/api/investigations/{inv_id}/events") as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            async for text in stream.aiter_text():
                chunks.append(text)
        frames = parse_sse("".join(chunks))
        inv = (await client.get(f"/api/investigations/{inv_id}")).json()
        report = await client.get(f"/api/investigations/{inv_id}/report.md")
        listed = (await client.get("/api/investigations", params={"mode": "replay"})).json()
    events = [f["event"] for f in frames]
    assert events[0] == "investigation_started" and events[-1] == "investigation_finished"
    assert [f["data"]["seq"] for f in frames] == list(range(1, len(frames) + 1))
    jsonschema.validate(inv, _schema("Investigation"))
    checks = score_investigation(scenario, Investigation.model_validate(inv), events)
    assert all(c.passed for c in checks), [c for c in checks if not c.passed]
    assert report.status_code == 200 and "v1.8.2" in report.text
    assert [i["id"] for i in listed["items"]] == [inv_id]
    # the store kept every event (a restarted process replays the same stream)
    assert len(await InvestigationStore.from_settings(settings).events(inv_id)) == len(frames)


async def test_list_pagination_and_dashboard_on_postgres(
    settings: Settings, store: InvestigationStore
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    severities = ["critical", "high", "none", "low", "critical", "medium", "high"]
    for i, severity in enumerate(severities):
        await store.save(
            Investigation(
                id=f"inv-pg{i}",
                incident=Incident(
                    title=f"Incident {i}",
                    service="order-service" if i % 2 else "payment-service",
                ),
                status=InvestigationStatus.FAILED if i == 3 else InvestigationStatus.COMPLETED,
                report=InvestigationReport(
                    summary=f"summary {i}", severity=severity, confidence=0.5 + i / 20
                ),
                created_at=now - timedelta(hours=(i // 2) * 7),  # pairs share a timestamp
                completed_at=now,
                duration_ms=60_000 * (i + 1),
                mode="demo",
            )
        )
    ctx = make_context(settings, store)
    async with api_client(ctx) as client:
        ids: list[str] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = (await client.get("/api/investigations", params=params)).json()
            ids += [i["id"] for i in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        orders = (await client.get("/api/investigations?service=order-service")).json()
        critical = (await client.get("/api/investigations?severity=critical")).json()
        failed = (await client.get("/api/investigations?status=failed")).json()
        dashboard = (await client.get("/api/dashboard/summary?days=14")).json()
    assert sorted(ids) == sorted(f"inv-pg{i}" for i in range(7)) and len(set(ids)) == 7
    assert {i["id"] for i in orders["items"]} == {"inv-pg1", "inv-pg3", "inv-pg5"}
    assert {i["id"] for i in critical["items"]} == {"inv-pg0", "inv-pg4"}
    assert [i["id"] for i in failed["items"]] == ["inv-pg3"]
    reloaded = await store.search(limit=100)
    expected = dashboard_summary(reloaded, days=14, now=datetime.now(UTC))
    assert dashboard["totals"] == expected["totals"]
    assert dashboard["totals"] == {
        "investigations": 7,
        "open": 0,
        "root_cause_found": 0,
        "no_incident": 1,
        "failed": 1,
    }
    assert dashboard["mttr_minutes"] == expected["mttr_minutes"]
    assert sum(d["investigations"] for d in dashboard["by_day"]) == 7


async def test_ticket_draft_approved_end_to_end_via_mock_tickets(
    settings: Settings, store: InvestigationStore
) -> None:
    """Draft -> approve -> the real ApprovalExecutor writes to mock-tickets-mcp."""
    approvals = ApprovalService.from_settings(settings)  # Postgres (temp schema)
    ctx = make_context(
        settings,
        store,
        approvals=approvals,
        executor_factory=default_executor_factory(settings),
    )
    try:
        async with api_client(ctx) as client:
            created = await client.post(
                "/api/investigations", json={"question": "x", "scenario": "S1"}
            )
            inv_id = created.json()["id"]
            await wait_until_idle(ctx)
            drafted = await client.post(f"/api/investigations/{inv_id}/tickets/draft")
            assert drafted.status_code == 201, drafted.text
            approval_id = drafted.json()["approval_id"]
            pending = (await client.get("/api/approvals?status=pending")).json()
            approved = await client.post(
                f"/api/approvals/{approval_id}/approve", json={"by": "pytest-api"}
            )
        assert [p["id"] for p in pending] == [approval_id]
        body = approved.json()
        assert approved.status_code == 200
        assert body["status"] == "executed", body.get("error")
        assert str(body["result"]["issue"]["key"]).startswith("OPS-")
        assert approvals.get(approval_id).status.value == "executed"  # persisted in Postgres
    finally:
        PostgresTicketSeeder(default_dsn()).seed(FIXED_NOW)


async def test_scenarios_forbidden_without_the_env_flag(
    settings: Settings, store: InvestigationStore
) -> None:
    ctx = make_context(settings, store)
    async with api_client(ctx) as client:
        listed = await client.get("/api/scenarios")
        inject = await client.post("/api/scenarios/S1/inject")
        revert = await client.post("/api/scenarios/revert")
    assert listed.status_code == 200 and len(listed.json()) == 6
    assert inject.status_code == 403 and revert.status_code == 403


def _schema(name: str) -> dict[str, Any]:
    import json

    loaded: dict[str, Any] = json.loads(
        (REPO_ROOT / "docs" / "schemas" / f"{name}.schema.json").read_text()
    )
    return loaded
