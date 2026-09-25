"""Ingest a markdown knowledge base into Postgres full-text search (no models, no embeddings).

    aiops knowledge ingest --path knowledge-base

Idempotent: a document is re-chunked only when its content hash changes (the
hash includes the chunker version), and documents whose files were removed are
deleted. Only paths under the ingested prefix are touched, so several roots can
share one schema.

Ranking weights (``ts_rank_cd``): A = title + heading path, B = body,
C = tags / services / alert names from the front matter.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from aiops.knowledge.markdown import (
    CHUNKER_VERSION,
    Chunk,
    Document,
    chunk_document,
    parse_document,
)

#: Text search configuration; knowledge-mcp must use the same one.
TS_CONFIG = "english"
DEFAULT_SCHEMA = "knowledge"
SKIP_FILES = frozenset({"README.md"})
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

SCHEMA_DDL = """
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.documents (
    path         text PRIMARY KEY,
    title        text NOT NULL,
    doc_type     text NOT NULL,
    services     text[] NOT NULL DEFAULT '{{}}',
    tags         text[] NOT NULL DEFAULT '{{}}',
    metadata     jsonb NOT NULL DEFAULT '{{}}',
    content      text NOT NULL,
    content_hash text NOT NULL,
    ingested_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {schema}.chunks (
    id           bigserial PRIMARY KEY,
    doc_path     text NOT NULL REFERENCES {schema}.documents(path) ON DELETE CASCADE,
    ordinal      integer NOT NULL,
    heading      text NOT NULL,
    heading_path text NOT NULL,
    anchor       text NOT NULL,
    content      text NOT NULL,
    tsv          tsvector NOT NULL,
    UNIQUE (doc_path, ordinal)
);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON {schema}.chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS documents_services_idx ON {schema}.documents USING gin (services);
CREATE INDEX IF NOT EXISTS documents_tags_idx ON {schema}.documents USING gin (tags);
"""


class IngestError(Exception):
    """The knowledge base or the database can't be ingested. Message is for humans."""


def default_conninfo() -> str:
    """``KNOWLEDGE_DATABASE_URL``, else the local stack's POSTGRES_* settings."""
    url = os.environ.get("KNOWLEDGE_DATABASE_URL", "").strip()
    if url:
        return url
    return make_conninfo(
        host=os.environ.get("POSTGRES_HOST") or "localhost",
        port=os.environ.get("POSTGRES_PORT") or "15432",
        user=os.environ.get("POSTGRES_USER") or "aiops",
        password=os.environ.get("POSTGRES_PASSWORD") or "aiops-local-only",
        dbname=os.environ.get("POSTGRES_DB") or "aiops",
        connect_timeout="5",
    )


def content_hash(text: str) -> str:
    return hashlib.sha256(f"chunker-v{CHUNKER_VERSION}\n{text}".encode()).hexdigest()


# --------------------------------------------------------------------------- scanning


@dataclass(frozen=True)
class SourceDoc:
    document: Document
    hash: str


def scan(root: Path, prefix: str) -> dict[str, SourceDoc]:
    """All ``*.md`` under ``root`` (README.md excluded), keyed by ``prefix/relative/path.md``."""
    if not root.is_dir():
        raise IngestError(f"Knowledge base directory not found: {root}")
    found: dict[str, SourceDoc] = {}
    for file in sorted(root.rglob("*.md")):
        relative = file.relative_to(root)
        if file.name in SKIP_FILES or any(part.startswith(".") for part in relative.parts):
            continue
        path = f"{prefix}/{relative.as_posix()}" if prefix else relative.as_posix()
        text = file.read_text(encoding="utf-8")
        try:
            document = parse_document(path, text)
        except ValueError as exc:
            raise IngestError(f"{path}: {exc}") from exc
        found[path] = SourceDoc(document, content_hash(text))
    return found


@dataclass
class IngestPlan:
    add: list[str] = field(default_factory=list)
    update: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)


def plan_changes(existing: dict[str, str], found: dict[str, str]) -> IngestPlan:
    """existing/found: path -> content hash."""
    plan = IngestPlan()
    for path, digest in sorted(found.items()):
        if path not in existing:
            plan.add.append(path)
        elif existing[path] != digest:
            plan.update.append(path)
        else:
            plan.unchanged.append(path)
    plan.delete = sorted(set(existing) - set(found))
    return plan


# --------------------------------------------------------------------------- store


@dataclass
class IngestReport:
    plan: IngestPlan
    chunks_written: int
    total_docs: int
    total_chunks: int


class KnowledgeStore:
    """Writes the ``knowledge`` schema. The only writer; knowledge-mcp only reads."""

    def __init__(self, conninfo: str | None = None, schema: str = DEFAULT_SCHEMA) -> None:
        if not _IDENTIFIER.match(schema):
            raise IngestError(f"Invalid schema name '{schema}'")
        self.conninfo = conninfo or default_conninfo()
        self.schema = schema

    def _sql(self, template: str) -> sql.Composed:
        return sql.SQL(template).format(schema=sql.Identifier(self.schema))

    def connect(self) -> psycopg.Connection[tuple[object, ...]]:
        try:
            return psycopg.connect(self.conninfo)
        except psycopg.OperationalError as exc:
            raise IngestError(
                f"Cannot connect to Postgres: {exc}".strip()
                + "\nIs the stack up (make infra-up)? Override with KNOWLEDGE_DATABASE_URL."
            ) from exc

    def ensure_schema(self, conn: psycopg.Connection[tuple[object, ...]]) -> None:
        conn.execute(self._sql(SCHEMA_DDL))

    def existing_hashes(
        self, conn: psycopg.Connection[tuple[object, ...]], prefix: str
    ) -> dict[str, str]:
        rows = conn.execute(
            self._sql(
                "SELECT path, content_hash FROM {schema}.documents "
                "WHERE %(prefix)s = '' OR starts_with(path, %(prefix)s || '/')"
            ),
            {"prefix": prefix},
        ).fetchall()
        return {str(path): str(digest) for path, digest in rows}

    def upsert(
        self, conn: psycopg.Connection[tuple[object, ...]], source: SourceDoc, chunks: list[Chunk]
    ) -> None:
        doc = source.document
        conn.execute(
            self._sql(
                "INSERT INTO {schema}.documents "
                "(path, title, doc_type, services, tags, metadata, content, content_hash, ingested_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (path) DO UPDATE SET title = EXCLUDED.title, "
                "doc_type = EXCLUDED.doc_type, services = EXCLUDED.services, tags = EXCLUDED.tags, "
                "metadata = EXCLUDED.metadata, content = EXCLUDED.content, "
                "content_hash = EXCLUDED.content_hash, ingested_at = now()"
            ),
            (
                doc.path,
                doc.title,
                doc.doc_type,
                list(doc.services),
                list(doc.tags),
                json.dumps(doc.metadata, default=str),
                doc.body.strip(),
                source.hash,
            ),
        )
        conn.execute(self._sql("DELETE FROM {schema}.chunks WHERE doc_path = %s"), (doc.path,))
        keywords = " ".join(
            [*doc.tags, *doc.services, *(str(a) for a in _as_list(doc.metadata.get("alerts")))]
        )
        with conn.cursor() as cur:
            cur.executemany(
                self._sql(
                    "INSERT INTO {schema}.chunks "
                    "(doc_path, ordinal, heading, heading_path, anchor, content, tsv) "
                    "VALUES (%(path)s, %(ordinal)s, %(heading)s, %(heading_path)s, %(anchor)s, %(content)s, "
                    "setweight(to_tsvector(%(cfg)s::regconfig, %(title)s || ' ' || %(heading_path)s), 'A') || "
                    "setweight(to_tsvector(%(cfg)s::regconfig, %(content)s), 'B') || "
                    "setweight(to_tsvector(%(cfg)s::regconfig, %(keywords)s), 'C'))"
                ),
                [
                    {
                        "path": doc.path,
                        "ordinal": c.ordinal,
                        "heading": c.heading,
                        "heading_path": c.heading_path,
                        "anchor": c.anchor,
                        "content": c.content,
                        "cfg": TS_CONFIG,
                        "title": doc.title,
                        "keywords": keywords,
                    }
                    for c in chunks
                ],
            )

    def delete(self, conn: psycopg.Connection[tuple[object, ...]], paths: list[str]) -> None:
        if paths:
            conn.execute(self._sql("DELETE FROM {schema}.documents WHERE path = ANY(%s)"), (paths,))

    def totals(self, conn: psycopg.Connection[tuple[object, ...]]) -> tuple[int, int]:
        row = conn.execute(
            self._sql(
                "SELECT (SELECT count(*) FROM {schema}.documents), (SELECT count(*) FROM {schema}.chunks)"
            )
        ).fetchone()
        return (int(str(row[0])), int(str(row[1]))) if row else (0, 0)

    def ingest(self, root: Path, prefix: str | None = None) -> IngestReport:
        """Sync ``root`` into the store in one transaction."""
        prefix = root.name if prefix is None else prefix.strip("/")
        sources = scan(root, prefix)
        written = 0
        with self.connect() as conn, conn.transaction():
            self.ensure_schema(conn)
            plan = plan_changes(
                self.existing_hashes(conn, prefix), {p: s.hash for p, s in sources.items()}
            )
            for path in [*plan.add, *plan.update]:
                chunks = chunk_document(sources[path].document)
                self.upsert(conn, sources[path], chunks)
                written += len(chunks)
            self.delete(conn, plan.delete)
            total_docs, total_chunks = self.totals(conn)
        return IngestReport(
            plan=plan, chunks_written=written, total_docs=total_docs, total_chunks=total_chunks
        )


def _as_list(value: object) -> Iterator[object]:
    if isinstance(value, list):
        yield from value
    elif value:
        yield value
