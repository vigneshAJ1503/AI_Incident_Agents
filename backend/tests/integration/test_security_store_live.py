"""PR-042: no raw secret reaches the Postgres evidence store or the Postgres audit table.

Runs a full leaky investigation through the API into a throwaway schema of the compose
Postgres (127.0.0.1:15432), then dumps every row of every table in that schema.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

from aiops.core.config import load_settings
from aiops.core.guardrails.audit import AuditRecord, JsonlAuditSink
from aiops.core.guardrails.redaction import SUPPORTED_KINDS, Redactor
from aiops.core.models import ToolCall
from aiops.store.db import StoreError, default_database_url, sync_url
from aiops.store.repository import InvestigationStore
from aiops.store.sync_stores import SqlAuditSink
from tests.security.leaky import assert_no_secrets
from tests.security.payloads import leaky_log_line
from tests.security.test_secrets_e2e import run_leaky
from tests.unit.test_api import CONFIG, with_llm_key

pytestmark = pytest.mark.integration


@pytest.fixture
def schema() -> Iterator[str]:
    name = f"security_test_{uuid4().hex[:8]}"
    yield name
    engine = sa.create_engine(sync_url(default_database_url()))
    with engine.begin() as conn:
        conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
    engine.dispose()


def dump_schema(schema: str) -> list[list[tuple[str, ...]]]:
    engine = sa.create_engine(sync_url(default_database_url()))
    with engine.connect() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                sa.text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
                {"s": schema},
            )
        ]
        rows = [
            [
                tuple(str(v) for v in row)
                for row in conn.execute(sa.text(f'SELECT * FROM "{schema}"."{t}"'))  # noqa: S608
            ]
            for t in tables
        ]
    engine.dispose()
    return rows


async def test_postgres_store_and_audit_hold_no_raw_secrets(tmp_path: Path, schema: str) -> None:
    store = InvestigationStore(default_database_url(), schema)
    try:
        store.migrate()
    except StoreError as exc:
        pytest.skip(f"compose Postgres not reachable: {exc}")
    await run_leaky(tmp_path, with_llm_key(load_settings("local", CONFIG)), store)

    # The Postgres audit sink stores the (redacted) record as it is given.
    redactor = Redactor(SUPPORTED_KINDS)
    sink = SqlAuditSink(
        default_database_url(), schema, fallback=JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    call = ToolCall(
        agent="logs",
        capability="logs",
        tool="search_logs",
        arguments=redactor.data({"query": leaky_log_line()}),
    )
    sink.record(AuditRecord(request_id="req-1", tool_call=call, redactions=dict(redactor.counts)))

    dump = dump_schema(schema)
    assert sum(len(rows) for rows in dump) > 3
    assert_no_secrets(f"Postgres schema {schema}", dump)
