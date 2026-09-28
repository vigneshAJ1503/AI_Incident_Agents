"""No raw secret survives a whole investigation: LLM input, audit log, evidence store
(SQLite here; Postgres in tests/integration/test_security_store_live.py), SSE and the API."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from aiops.core.config import Settings, load_settings
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.store.repository import InvestigationStore
from tests.api_support import api_client, make_context, parse_sse
from tests.security.leaky import (
    assert_no_secrets,
    leaky_llm,
    leaky_orchestrator_factory,
)
from tests.security.payloads import leaky_log_line, secrets
from tests.unit.test_api import CONFIG, with_llm_key


@pytest.fixture(scope="module")
def settings() -> Settings:
    return with_llm_key(load_settings("local", CONFIG))


def test_the_payload_really_contains_the_secrets() -> None:
    line = leaky_log_line()
    assert all(value in line for value in secrets().values())


async def run_leaky(tmp_path: Path, settings: Settings, store: InvestigationStore) -> None:
    llm = leaky_llm()
    audit = MemoryAuditSink()
    factory = leaky_orchestrator_factory(tmp_path, llm, audit)
    ctx = make_context(settings, store, orchestrator_factory=factory)
    async with api_client(ctx) as client:
        created = await client.post(
            "/api/investigations",
            json={"question": "payment-service is returning 500 errors", "mode": "live"},
        )
        assert created.status_code == 202, created.text
        inv_id = created.json()["id"]
        stream = await client.get(f"/api/investigations/{inv_id}/events")
        frames = parse_sse(stream.text)
        assert frames[-1]["event"] == "investigation_finished"
        assert any(f["event"] == "evidence_added" for f in frames)
        assert_no_secrets("SSE stream", stream.text)

        inv = await client.get(f"/api/investigations/{inv_id}")
        assert inv.status_code == 200
        assert "[REDACTED:" in inv.text  # the evidence is there, redacted
        assert_no_secrets("GET /investigations/{id}", inv.text)
        listing = await client.get("/api/investigations")
        assert_no_secrets("GET /investigations", listing.text)
        report = await client.get(f"/api/investigations/{inv_id}/report.md")
        assert_no_secrets("report.md", report.text)
        assert_no_secrets("dashboard", (await client.get("/api/dashboard/summary")).text)

    # What the model saw.
    assert llm.requests
    for request in llm.requests:
        assert_no_secrets("LLM input", [m.content for m in request["messages"]])
    # The audit trail.
    assert audit.records and any(r.redactions for r in audit.records)
    assert_no_secrets("audit log", [r.model_dump(mode="json") for r in audit.records])


async def test_no_raw_secret_in_llm_audit_store_sse_or_api(
    tmp_path: Path, settings: Settings
) -> None:
    url = f"sqlite:///{tmp_path / 'leaky.db'}"
    store = InvestigationStore(url)
    store.migrate()
    await run_leaky(tmp_path, settings, store)
    # Every stored row, raw.
    engine = sa.create_engine(url)
    with engine.connect() as conn:
        tables = [
            r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))
        ]
        dump = [
            [tuple(str(v) for v in row) for row in conn.execute(sa.text(f'SELECT * FROM "{t}"'))]  # noqa: S608
            for t in tables
        ]
    engine.dispose()
    assert any(dump)
    assert_no_secrets("SQLite store", dump)
