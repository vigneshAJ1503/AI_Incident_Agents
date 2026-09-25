"""Requires `make infra-up ingest-knowledge` and knowledge-mcp on :8108. Run: make test-integration"""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from aiops.core.config import load_settings
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.knowledge.ingest import KnowledgeStore, default_conninfo
from aiops.mcp.registry import MCPRegistry

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[3]
CONFIG = REPO / "config"


def test_ingest_is_idempotent_and_deletes_removed_docs(tmp_path: Path) -> None:
    kb = tmp_path / "knowledge-base"
    shutil.copytree(REPO / "knowledge-base", kb)
    schema = f"knowledge_it_{uuid4().hex[:8]}"
    store = KnowledgeStore(schema=schema)
    try:
        first = store.ingest(kb)
        assert len(first.plan.add) == first.total_docs >= 12 and first.total_chunks > 50

        again = store.ingest(kb)
        assert again.plan.add == again.plan.update == again.plan.delete == []
        assert again.chunks_written == 0 and again.total_chunks == first.total_chunks

        redis = kb / "runbooks" / "redis-outage.md"
        redis.write_text(redis.read_text() + "\n## Notes\nSentinel failover drill.\n")
        (kb / "runbooks" / "pod-crashloop.md").unlink()
        changed = store.ingest(kb)
        assert changed.plan.update == ["knowledge-base/runbooks/redis-outage.md"]
        assert changed.plan.delete == ["knowledge-base/runbooks/pod-crashloop.md"]
        assert changed.total_docs == first.total_docs - 1
        with psycopg.connect(default_conninfo()) as conn:
            orphans = conn.execute(
                sql.SQL(
                    "SELECT count(*) FROM {}.chunks WHERE doc_path LIKE '%%crashloop%%'"
                ).format(sql.Identifier(schema))
            ).fetchone()
            assert orphans == (0,)
    finally:
        with psycopg.connect(default_conninfo(), autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize(
    ("symptoms", "runbook"),
    [
        (
            "Database connection timeout: could not acquire a connection from the pool",
            "database-connection-pool.md",
        ),
        ("java.lang.OutOfMemoryError: Java heap space", "memory-leak-oom.md"),
        ("Timeout calling inventory-service dependency timeouts", "dependency-timeouts.md"),
        ("Request queue depth high ready replicas 1/3", "bad-deployment-rollback.md"),
        ("Redis connection refused ECONNREFUSED", "redis-outage.md"),
    ],
)
async def test_capability_search_finds_scenario_runbooks(symptoms: str, runbook: str) -> None:
    settings = load_settings("local", CONFIG)
    audit = MemoryAuditSink()
    async with MCPRegistry(settings, audit=audit).toolset("knowledge", agent="it") as tools:
        assert {s.name for s in await tools.specs()} == {"search", "get_doc", "list_docs"}
        outcome = await tools.call("search", {"query": symptoms, "k": 5})
        assert outcome.ok, outcome.content
        runbooks = [r["path"] for r in outcome.data["results"] if r["doc_type"] == "runbook"]
        assert runbooks[0] == f"knowledge-base/runbooks/{runbook}"
        blocked = await tools.call("drop_schema", {})
        assert not blocked.ok and "not allowed" in blocked.content
    assert [r.tool_call.status for r in audit.records] == ["ok", "blocked"]
