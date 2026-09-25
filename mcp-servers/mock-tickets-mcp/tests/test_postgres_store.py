"""PostgresTicketStore against the local Postgres (make infra-up), in a throwaway schema.

uv run pytest -m integration -o addopts=""
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from mock_tickets_mcp.config import ServerSettings
from mock_tickets_mcp.jql import parse, search
from mock_tickets_mcp.store import NewIssue, PostgresTicketStore, StoreError
from tests.sample import NOW, issues

pytestmark = pytest.mark.integration


def settings() -> ServerSettings:
    return ServerSettings(
        pg_password=os.environ.get("PG_PASSWORD", "aiops-local-only"),
        schema=f"tickets_test_{uuid.uuid4().hex[:8]}",
    )


async def test_roundtrip_in_throwaway_schema() -> None:
    s = settings()
    store = PostgresTicketStore(s.dsn, s.schema)
    try:
        await store.setup()
        await store.setup()  # idempotent
        # Insert the sample backlog the way the seeder does.
        async with await psycopg.AsyncConnection.connect(s.dsn) as conn:
            for i in issues():
                await conn.execute(
                    f'INSERT INTO "{s.schema}".issues (key, project, number, summary, description, '  # noqa: S608
                    "issue_type, status, status_category, priority, resolution, labels, components, "
                    "reporter, assignee, created, updated, resolved) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        i.key,
                        i.project,
                        i.number,
                        i.summary,
                        i.description,
                        i.issue_type,
                        i.status,
                        i.status_category,
                        i.priority,
                        i.resolution,
                        i.labels,
                        i.components,
                        i.reporter,
                        i.assignee,
                        i.created,
                        i.updated,
                        i.resolved,
                    ],
                )
        ops = await store.list_issues(["OPS"])
        assert sorted(i.key for i in ops) == ["OPS-12", "OPS-2", "OPS-3", "OPS-7"]
        hits = search(ops, parse('labels = payment-service AND text ~ "500"'), NOW)
        assert [i.key for i in hits] == ["OPS-12"]

        created = await store.create_issue(
            NewIssue(project="OPS", summary="new", issue_type="Bug", created=NOW, labels=["aiops"])
        )
        assert created.key == "OPS-13" and created.status == "Open"
        await store.add_comment("OPS-13", "hello", "aiops-agent", NOW)
        updated = await store.update_issue("OPS-13", {"summary": "renamed", "labels": ["x"]}, NOW)
        assert updated.summary == "renamed" and updated.labels == ["x"]
        assert updated.comments[0].body == "hello"
        with pytest.raises(StoreError):
            await store.add_comment("OPS-999", "x", "a", NOW)
    finally:
        async with await psycopg.AsyncConnection.connect(s.dsn, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{s.schema}" CASCADE')
