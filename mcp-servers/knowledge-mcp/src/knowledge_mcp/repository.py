"""Read-only access to the ``knowledge`` schema written by ``aiops knowledge ingest``.

Every call opens a connection with ``default_transaction_read_only=on`` and a
statement timeout, and runs in a ``READ ONLY`` transaction: even a bug here
can't write. The server talks to the ``KnowledgeRepository`` protocol, so unit
tests use an in-memory fake instead of Postgres.

Ranking (``search``): each chunk's tsvector has weights A = title + heading path,
B = body, C = tags/services/alert names. With ``match="any"`` the query's
lexemes are OR-ed, and chunks are ordered by
``score = 0.7 * coverage + 0.3 * ts_rank_cd / (ts_rank_cd + 1)``, where coverage is
the fraction of query lexemes the chunk contains. Long symptom lists (log
patterns, alert names) therefore rank the section that matches most of them first.
With ``match="all"`` the query uses ``websearch_to_tsquery`` syntax (all words,
"quoted phrases", ``or``, ``-exclude``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row

from knowledge_mcp.config import ServerSettings

Match = Literal["any", "all"]

HEADLINE_OPTIONS = (
    'MaxWords=35, MinWords=12, MaxFragments=2, FragmentDelimiter=" … ", StartSel=**, StopSel=**'
)


class RepositoryError(Exception):
    """The knowledge store is unavailable or not initialized."""


@dataclass(frozen=True)
class SearchHit:
    path: str
    title: str
    doc_type: str
    services: list[str]
    heading: str
    heading_path: str
    anchor: str
    snippet: str
    score: float
    matched_terms: list[str]


@dataclass(frozen=True)
class SearchResult:
    terms: list[str]  # the query's lexemes (stemmed), for coverage
    total_matches: int  # chunks matching before k / per-doc caps
    hits: list[SearchHit] = field(default_factory=list)


@dataclass(frozen=True)
class DocSummary:
    path: str
    title: str
    doc_type: str
    services: list[str]
    tags: list[str]


@dataclass(frozen=True)
class Doc:
    path: str
    title: str
    doc_type: str
    services: list[str]
    tags: list[str]
    metadata: dict[str, Any]
    content: str
    outline: list[dict[str, str]]  # [{"heading_path", "anchor"}] in document order


class KnowledgeRepository(Protocol):
    async def search(
        self,
        query: str,
        *,
        k: int,
        services: list[str],
        tags: list[str],
        match: Match,
        max_per_doc: int,
    ) -> SearchResult: ...

    async def get_doc(self, path: str) -> Doc | None: ...

    async def list_docs(
        self, *, doc_type: str | None, service: str | None, limit: int
    ) -> tuple[list[DocSummary], int]: ...


_TERMS_SQL = """
SELECT coalesce(array_agg(DISTINCT lex ORDER BY lex), '{{}}') AS terms
FROM unnest(tsvector_to_array(to_tsvector(%(cfg)s::regconfig, %(query)s))) AS lex
"""

_SEARCH_SQL = """
WITH q AS (
    SELECT CASE WHEN %(match)s = 'all' THEN websearch_to_tsquery(%(cfg)s::regconfig, %(query)s)
                ELSE to_tsquery('simple', (SELECT string_agg(quote_literal(x), ' | ')
                                           FROM unnest(%(terms)s::text[]) AS x))
           END AS query
),
hits AS (
    SELECT c.doc_path, c.heading, c.heading_path, c.anchor, c.content,
           d.title, d.doc_type, d.services,
           ts_rank_cd(c.tsv, q.query, 1) AS rank,
           ARRAY(SELECT x FROM unnest(%(terms)s::text[]) AS x
                 WHERE c.tsv @@ to_tsquery('simple', quote_literal(x))) AS matched
    FROM {schema}.chunks AS c
    JOIN {schema}.documents AS d ON d.path = c.doc_path
    CROSS JOIN q
    WHERE c.tsv @@ q.query
      AND (cardinality(%(services)s::text[]) = 0 OR d.services && %(services)s::text[])
      AND (cardinality(%(tags)s::text[]) = 0 OR d.tags && %(tags)s::text[])
),
scored AS (
    SELECT *, 0.7 * cardinality(matched)::float8 / greatest(cardinality(%(terms)s::text[]), 1)
              + 0.3 * rank / (rank + 1) AS score
    FROM hits
),
ranked AS (
    SELECT *, row_number() OVER (PARTITION BY doc_path ORDER BY score DESC, heading_path) AS nth
    FROM scored
)
SELECT doc_path, title, doc_type, services, heading, heading_path, anchor, score, matched,
       ts_headline(%(cfg)s::regconfig, content, (SELECT query FROM q), %(headline)s) AS snippet,
       (SELECT count(*) FROM hits) AS total
FROM ranked
WHERE nth <= %(per_doc)s
ORDER BY score DESC, doc_path, heading_path
LIMIT %(k)s
"""

_GET_DOC_SQL = """
SELECT path, title, doc_type, services, tags, metadata, content
FROM {schema}.documents WHERE path = %(path)s
"""

_OUTLINE_SQL = """
SELECT heading_path, anchor FROM {schema}.chunks WHERE doc_path = %(path)s
GROUP BY heading_path, anchor ORDER BY min(ordinal)
"""

_LIST_SQL = """
SELECT path, title, doc_type, services, tags, count(*) OVER () AS total
FROM {schema}.documents
WHERE (%(doc_type)s::text IS NULL OR doc_type = %(doc_type)s::text)
  AND (%(service)s::text IS NULL OR %(service)s::text = ANY(services))
ORDER BY path
LIMIT %(limit)s
"""


class PostgresRepository:
    def __init__(self, settings: ServerSettings) -> None:
        self._settings = settings
        self._schema = sql.Identifier(settings.schema)

    def _sql(self, template: str) -> sql.Composed:
        return sql.SQL(template).format(schema=self._schema)

    @asynccontextmanager
    async def _read_only(self) -> AsyncIterator[psycopg.AsyncConnection[DictRow]]:
        s = self._settings
        try:
            conn = await psycopg.AsyncConnection.connect(
                s.conninfo,
                options=(
                    f"-c default_transaction_read_only=on -c statement_timeout={int(s.statement_timeout_ms)}"
                ),
                connect_timeout=s.connect_timeout_s,
                application_name="knowledge-mcp",
                row_factory=dict_row,
            )
        except psycopg.OperationalError as exc:
            raise RepositoryError(f"knowledge store unavailable: {exc}".strip()) from exc
        try:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION READ ONLY")
                yield conn
        except psycopg.errors.UndefinedTable as exc:
            raise RepositoryError(
                "knowledge index not found: run `make ingest-knowledge` first"
            ) from exc
        except psycopg.Error as exc:
            raise RepositoryError(f"knowledge store error: {exc}".strip()) from exc
        finally:
            await conn.close()

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
        params: dict[str, Any] = {"cfg": self._settings.ts_config, "query": query}
        async with self._read_only() as conn:
            row = await (await conn.execute(self._sql(_TERMS_SQL), params)).fetchone()
            terms = [str(t) for t in (row or {}).get("terms") or []]
            if not terms:
                return SearchResult(terms=[], total_matches=0)
            params.update(
                terms=terms,
                match=match,
                services=services,
                tags=tags,
                per_doc=max_per_doc,
                k=k,
                headline=HEADLINE_OPTIONS,
            )
            rows = await (await conn.execute(self._sql(_SEARCH_SQL), params)).fetchall()
        hits = [
            SearchHit(
                path=r["doc_path"],
                title=r["title"],
                doc_type=r["doc_type"],
                services=list(r["services"]),
                heading=r["heading"],
                heading_path=r["heading_path"],
                anchor=r["anchor"],
                snippet=r["snippet"],
                score=round(float(r["score"]), 4),
                matched_terms=list(r["matched"]),
            )
            for r in rows
        ]
        total = int(rows[0]["total"]) if rows else 0
        return SearchResult(terms=terms, total_matches=total, hits=hits)

    async def get_doc(self, path: str) -> Doc | None:
        async with self._read_only() as conn:
            row = await (await conn.execute(self._sql(_GET_DOC_SQL), {"path": path})).fetchone()
            if row is None:
                return None
            outline = await (await conn.execute(self._sql(_OUTLINE_SQL), {"path": path})).fetchall()
        return Doc(
            path=row["path"],
            title=row["title"],
            doc_type=row["doc_type"],
            services=list(row["services"]),
            tags=list(row["tags"]),
            metadata=dict(row["metadata"] or {}),
            content=row["content"],
            outline=[{"heading_path": o["heading_path"], "anchor": o["anchor"]} for o in outline],
        )

    async def list_docs(
        self, *, doc_type: str | None, service: str | None, limit: int
    ) -> tuple[list[DocSummary], int]:
        params = {"doc_type": doc_type, "service": service, "limit": limit}
        async with self._read_only() as conn:
            rows = await (await conn.execute(self._sql(_LIST_SQL), params)).fetchall()
        docs = [
            DocSummary(
                path=r["path"],
                title=r["title"],
                doc_type=r["doc_type"],
                services=list(r["services"]),
                tags=list(r["tags"]),
            )
            for r in rows
        ]
        return docs, int(rows[0]["total"]) if rows else 0
