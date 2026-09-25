"""MCP tools: search, get_doc, list_docs (all read-only)."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from knowledge_mcp.config import ServerSettings
from knowledge_mcp.guards import (
    GuardError,
    validate_doc_path,
    validate_k,
    validate_query,
    validate_slugs,
)
from knowledge_mcp.repository import (
    KnowledgeRepository,
    Match,
    PostgresRepository,
    RepositoryError,
)

INSTRUCTIONS = """Read-only search over the team's runbooks and service docs (markdown in git).
Use `search` with symptoms (log messages, alert names, error types) to find the relevant
sections; results are sections with their heading path, a highlighted snippet and a score.
Then `get_doc` the best documents to read their Diagnosis / Mitigation / Rollback steps.
Document paths are repo-relative, e.g. knowledge-base/runbooks/redis-outage.md."""

MAX_PER_DOC = 5
DOC_TYPES = ("runbook", "service", "doc")


def create_server(
    settings: ServerSettings, repository: KnowledgeRepository | None = None
) -> MCPServer:
    repo = repository or PostgresRepository(settings)
    server = MCPServer("knowledge-mcp", instructions=INSTRUCTIONS, version="0.1.0")

    async def guarded[T](coro: Awaitable[T]) -> T:
        try:
            return await coro
        except RepositoryError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    async def search(
        query: str,
        k: int = 5,
        services: list[str] | None = None,
        tags: list[str] | None = None,
        match: Match = "any",
        max_per_doc: int = 3,
    ) -> dict[str, Any]:
        """Full-text search over runbook and service-doc sections, best first.

        Args:
            query: symptoms or keywords, e.g. 'could not acquire a connection from the pool HikariPool'.
            k: number of sections to return (1-10).
            services: only docs tagged with any of these services, e.g. ['payment-service'].
                Generic runbooks (no services) are excluded by this filter; search again without it.
            tags: only docs with any of these tags, e.g. ['database'].
            match: 'any' (default) ranks sections by how many query words they contain;
                'all' requires every word (web-search syntax: "quoted phrase", or, -exclude).
            max_per_doc: at most this many sections per document (1-5), for diverse results.
        """
        try:
            text = validate_query(query, settings.max_query_chars)
            limit = validate_k(k, settings.max_k)
            service_filter = validate_slugs(services, "services", settings.max_filter_items)
            tag_filter = validate_slugs(tags, "tags", settings.max_filter_items)
            if match not in ("any", "all"):
                raise GuardError("match must be 'any' or 'all'")
            if not 1 <= max_per_doc <= MAX_PER_DOC:
                raise GuardError(f"max_per_doc must be between 1 and {MAX_PER_DOC}")
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        result = await guarded(
            repo.search(
                text,
                k=limit,
                services=service_filter,
                tags=tag_filter,
                match=match,
                max_per_doc=max_per_doc,
            )
        )
        payload: dict[str, Any] = {
            "query": text,
            "match": match,
            "terms": result.terms,
            "filters": {"services": service_filter, "tags": tag_filter},
            "total_matches": result.total_matches,
            "returned": len(result.hits),
            "results": [{"rank": i, **asdict(h)} for i, h in enumerate(result.hits, start=1)],
        }
        if not result.terms:
            payload["note"] = "The query has no searchable words (only stop words or symbols)."
        return payload

    @server.tool()
    async def get_doc(path: str) -> dict[str, Any]:
        """Full markdown of one document, with its section outline (heading paths + anchors).

        Args:
            path: repo-relative path from search/list_docs, e.g. 'knowledge-base/runbooks/redis-outage.md'.
        """
        try:
            clean = validate_doc_path(path)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        doc = await guarded(repo.get_doc(clean))
        if doc is None:
            raise ToolError(f"document not found: {clean} (use list_docs to see what exists)")
        data = asdict(doc)
        content = doc.content
        data["truncated"] = len(content) > settings.max_doc_chars
        data["content"] = content[: settings.max_doc_chars]
        return data

    @server.tool()
    async def list_docs(doc_type: str | None = None, service: str | None = None) -> dict[str, Any]:
        """List indexed documents (path, title, type, services, tags).

        Args:
            doc_type: optional 'runbook', 'service' or 'doc'.
            service: optional service name; only docs tagged with it, e.g. 'payment-service'.
        """
        try:
            if doc_type is not None and doc_type not in DOC_TYPES:
                raise GuardError(f"doc_type must be one of {list(DOC_TYPES)}")
            service_filter = validate_slugs([service] if service else None, "service", 1)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        docs, total = await guarded(
            repo.list_docs(
                doc_type=doc_type,
                service=service_filter[0] if service_filter else None,
                limit=settings.max_list_docs,
            )
        )
        return {
            "total": total,
            "returned": len(docs),
            "truncated": total > len(docs),
            "documents": [asdict(d) for d in docs],
        }

    return server
