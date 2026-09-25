"""Against the live Postgres with the repo's knowledge base ingested.

make infra-up ingest-knowledge
cd mcp-servers/knowledge-mcp && uv run pytest -m integration -o addopts=""
"""

from __future__ import annotations

import os
from typing import Any

import psycopg
import pytest
from mcp import Client
from psycopg.conninfo import make_conninfo

from knowledge_mcp.config import ServerSettings
from knowledge_mcp.repository import PostgresRepository
from knowledge_mcp.server import create_server

pytestmark = pytest.mark.integration

CONNINFO = os.environ.get("KNOWLEDGE_DATABASE_URL") or make_conninfo(
    host="localhost",
    port=os.environ.get("POSTGRES_PORT", "15432"),
    user=os.environ.get("POSTGRES_USER", "aiops"),
    password=os.environ.get("POSTGRES_PASSWORD", "aiops-local-only"),
    dbname=os.environ.get("POSTGRES_DB", "aiops"),
)
SETTINGS = ServerSettings(conninfo=CONNINFO)
RUNBOOKS = "knowledge-base/runbooks"


async def search(args: dict[str, Any]) -> dict[str, Any]:
    async with Client(create_server(SETTINGS)) as client:
        result = await client.call_tool("search", args)
    assert not result.is_error, result.content
    data: dict[str, Any] = result.structured_content
    return data


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "Database connection timeout: could not acquire a connection from the pool HikariPool",
            "database-connection-pool.md",
        ),
        ("java.lang.OutOfMemoryError: Java heap space GC overhead", "memory-leak-oom.md"),
        ("Timeout calling inventory-service GET /api/v1/stock", "dependency-timeouts.md"),
        ("Request queue depth high ready replicas ImagePullBackOff", "bad-deployment-rollback.md"),
        ("Redis connection refused ECONNREFUSED cache unavailable", "redis-outage.md"),
        ("CrashLoopBackOff exit code", "pod-crashloop.md"),
    ],
)
async def test_symptoms_find_the_right_runbook(query: str, expected: str) -> None:
    data = await search({"query": query, "k": 5})
    runbooks = [r for r in data["results"] if r["doc_type"] == "runbook"]
    assert runbooks[0]["path"] == f"{RUNBOOKS}/{expected}"  # the best runbook
    assert "**" in runbooks[0]["snippet"]  # ts_headline highlights


async def test_service_filter_and_match_all() -> None:
    data = await search({"query": "connection pool", "services": ["inventory-service"], "k": 10})
    assert all("inventory-service" in r["services"] for r in data["results"])
    strict = await search({"query": '"heap space" -redis', "match": "all", "k": 10})
    assert {r["path"] for r in strict["results"]} == {f"{RUNBOOKS}/memory-leak-oom.md"}


async def test_get_doc_and_list_docs() -> None:
    async with Client(create_server(SETTINGS)) as client:
        doc = (
            await client.call_tool("get_doc", {"path": f"{RUNBOOKS}/redis-outage.md"})
        ).structured_content
        listing = (await client.call_tool("list_docs", {"doc_type": "service"})).structured_content
    assert doc["title"] == "Redis cache outage" and "## Mitigation" in doc["content"]
    assert any(o["anchor"] == "mitigation" for o in doc["outline"])
    assert {d["path"].rsplit("/", 1)[1] for d in listing["documents"]} >= {
        "payment-service.md",
        "order-service.md",
        "user-service.md",
        "inventory-service.md",
    }


async def test_connections_are_read_only() -> None:
    repo = PostgresRepository(SETTINGS)
    async with repo._read_only() as conn:
        row = await (await conn.execute("SHOW transaction_read_only")).fetchone()
        assert row is not None and row["transaction_read_only"] == "on"
    with (
        psycopg.connect(CONNINFO, options="-c default_transaction_read_only=on") as conn,
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
    ):
        conn.execute("DELETE FROM knowledge.chunks")
