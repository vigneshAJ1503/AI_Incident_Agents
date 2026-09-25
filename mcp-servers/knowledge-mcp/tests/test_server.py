"""Unit tests: the MCP tools over an in-memory repository (no Postgres needed)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from mcp import Client

from knowledge_mcp.config import ServerSettings
from knowledge_mcp.repository import (
    Doc,
    DocSummary,
    Match,
    RepositoryError,
    SearchHit,
    SearchResult,
)
from knowledge_mcp.server import create_server

SETTINGS = ServerSettings(max_k=10, max_query_chars=100, max_doc_chars=40)
POOL = "knowledge-base/runbooks/database-connection-pool.md"


def hit(path: str = POOL, score: float = 0.9) -> SearchHit:
    return SearchHit(
        path=path,
        title="Database connection pool exhaustion",
        doc_type="runbook",
        services=["payment-service"],
        heading="Symptoms",
        heading_path="Database connection pool exhaustion > Symptoms",
        anchor="symptoms",
        snippet="could not **acquire** a **connection**",
        score=score,
        matched_terms=["acquir", "connect"],
    )


@dataclass
class FakeRepository:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: bool = False

    async def search(
        self,
        query: str,
        *,
        k: int,
        services: list[str],
        tags: list[str],
        match: Match,
        max_per_doc: int,
    ) -> SearchResult:
        self.calls.append(
            (
                "search",
                {
                    "query": query,
                    "k": k,
                    "services": services,
                    "tags": tags,
                    "match": match,
                    "max_per_doc": max_per_doc,
                },
            )
        )
        if self.fail:
            raise RepositoryError("knowledge index not found: run `make ingest-knowledge` first")
        if query == "the of":
            return SearchResult(terms=[], total_matches=0)
        return SearchResult(terms=["acquir", "connect", "pool"], total_matches=7, hits=[hit()][:k])

    async def get_doc(self, path: str) -> Doc | None:
        self.calls.append(("get_doc", {"path": path}))
        if path != POOL:
            return None
        return Doc(
            path=POOL,
            title="Database connection pool exhaustion",
            doc_type="runbook",
            services=["payment-service"],
            tags=["database"],
            metadata={"owner": "platform-sre"},
            content="# Database connection pool exhaustion\n\n## Symptoms\n- timeouts\n" * 3,
            outline=[
                {
                    "heading_path": "Database connection pool exhaustion > Symptoms",
                    "anchor": "symptoms",
                }
            ],
        )

    async def list_docs(
        self, *, doc_type: str | None, service: str | None, limit: int
    ) -> tuple[list[DocSummary], int]:
        self.calls.append(("list_docs", {"doc_type": doc_type, "service": service, "limit": limit}))
        docs = [DocSummary(POOL, "DB pool", "runbook", ["payment-service"], ["database"])]
        return docs, 1


async def call(tool: str, args: dict[str, Any], repo: FakeRepository | None = None) -> Any:
    server = create_server(SETTINGS, repo or FakeRepository())
    async with Client(server) as client:
        return await client.call_tool(tool, args)


async def test_tools_listed() -> None:
    async with Client(create_server(SETTINGS, FakeRepository())) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"search", "get_doc", "list_docs"}


async def test_search_returns_ranked_sections_and_normalizes_filters() -> None:
    repo = FakeRepository()
    result = await call(
        "search",
        {
            "query": "  pool\x00 exhausted ",
            "k": 3,
            "services": ["Payment-Service", "payment-service"],
        },
        repo,
    )
    data = result.structured_content
    assert data["returned"] == 1 and data["total_matches"] == 7
    first = data["results"][0]
    assert first["rank"] == 1 and first["path"] == POOL
    assert first["heading_path"] == "Database connection pool exhaustion > Symptoms"
    assert first["snippet"] and first["score"] == 0.9
    assert repo.calls[0][1] == {
        "query": "pool  exhausted",
        "k": 3,
        "services": ["payment-service"],
        "tags": [],
        "match": "any",
        "max_per_doc": 3,
    }


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"query": ""}, "query is required"),
        ({"query": "x" * 101}, "too long"),
        ({"query": "pool", "k": 11}, "k must be between 1 and 10"),
        ({"query": "pool", "k": 0}, "k must be between"),
        ({"query": "pool", "services": ["a b"]}, "invalid services"),
        ({"query": "pool", "tags": [f"t{i}" for i in range(11)]}, "at most 10"),
        ({"query": "pool", "max_per_doc": 9}, "max_per_doc"),
    ],
)
async def test_search_input_validation(args: dict[str, Any], message: str) -> None:
    repo = FakeRepository()
    result = await call("search", args, repo)
    assert result.is_error
    assert message in result.content[0].text
    assert repo.calls == []  # rejected before touching the store


async def test_search_without_searchable_words_explains() -> None:
    data = (await call("search", {"query": "the of"})).structured_content
    assert data["results"] == [] and "no searchable words" in data["note"]


async def test_repository_errors_become_tool_errors() -> None:
    result = await call("search", {"query": "pool"}, FakeRepository(fail=True))
    assert result.is_error and "make ingest-knowledge" in result.content[0].text


async def test_get_doc_caps_content() -> None:
    data = (await call("get_doc", {"path": POOL})).structured_content
    assert data["title"] == "Database connection pool exhaustion"
    assert data["truncated"] is True and len(data["content"]) == SETTINGS.max_doc_chars
    assert data["outline"][0]["anchor"] == "symptoms"


@pytest.mark.parametrize(
    "path",
    ["../etc/passwd.md", "/etc/x.md", "knowledge-base/../../x.md", "a.txt", "a//b.md", "a b.md"],
)
async def test_get_doc_rejects_bad_paths(path: str) -> None:
    repo = FakeRepository()
    result = await call("get_doc", {"path": path}, repo)
    assert result.is_error and "invalid document path" in result.content[0].text
    assert repo.calls == []


async def test_get_doc_not_found() -> None:
    result = await call("get_doc", {"path": "knowledge-base/runbooks/nope.md"})
    assert result.is_error and "document not found" in result.content[0].text


async def test_list_docs_filters() -> None:
    repo = FakeRepository()
    data = (
        await call("list_docs", {"doc_type": "runbook", "service": "Payment-Service"}, repo)
    ).structured_content
    assert data["documents"][0]["path"] == POOL and data["truncated"] is False
    assert repo.calls[0][1] == {"doc_type": "runbook", "service": "payment-service", "limit": 500}
    bad = await call("list_docs", {"doc_type": "secret"})
    assert bad.is_error and "doc_type must be one of" in bad.content[0].text


def test_settings_reject_unsafe_identifiers() -> None:
    with pytest.raises(ValueError, match="invalid schema"):
        ServerSettings(schema="knowledge; drop table x")
