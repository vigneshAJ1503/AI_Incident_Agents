# ADR-0004: Runbook search with Postgres full-text search (no embeddings)

- **Status:** Accepted
- **Date:** 2026-09-26

## Context
The Knowledge agent (UC-09) must find the runbook sections that match an incident's symptoms: log patterns, alert names and error types. The usual approach is vector search over embeddings. That needs either a local embedding model, which the project forbids (no AI models run locally), or a paid or rate-limited hosted embedding API, which would break the zero-cost, offline and deterministic-CI goals. The knowledge base is small (tens of documents), written by engineers, and full of exact identifiers such as `HikariPool`, `ECONNREFUSED`, `OOMKilled` and alert names. Lexical search handles those better than embeddings do.

## Decision
- The markdown in `knowledge-base/` (front matter + headings) is the source of truth, versioned in git.
- `aiops knowledge ingest` chunks each document **by heading**. Every chunk keeps its heading path ("Database connection pool exhaustion > Diagnosis > 1. Confirm ..."). The chunks are upserted into Postgres (schema `knowledge`) with a `tsvector` weighted **A** = title + heading path, **B** = body, **C** = tags, services and alert names. Ingestion is idempotent (content hash, including the chunker version) and deletes removed documents.
- The custom `knowledge-mcp` server (read-only transactions, validated inputs, result caps) exposes `search`, `get_doc` and `list_docs`.
- Ranking for symptom queries (`match="any"`): OR the query's lexemes, then order by `0.7 × coverage + 0.3 × ts_rank_cd/(1+ts_rank_cd)`. *Coverage* is the share of query lexemes the section contains, so a pasted log line ranks the section that quotes it first. `match="all"` offers `websearch_to_tsquery` syntax for precise queries. Snippets come from `ts_headline`.
- Citations are links built from `capabilities.knowledge.settings.ui_link_template` (the GitHub blob URL of the doc, plus the section anchor).

## Consequences
- No model, no API cost, no network dependency: results are deterministic, so recorded fixtures and CI stay stable.
- Paraphrases with no shared words (for example "payments are sluggish" against a runbook that says "latency") match poorly. We mitigate this on the agent side: the Knowledge agent builds its queries from structured hints (log patterns, signal vocabulary, alert names) rather than only from the question, and runbooks follow a writing guide (`knowledge-base/README.md`) that quotes exact log lines and alert names.
- Adding `pg_trgm` (typo tolerance) or a hosted embedding re-ranker later is a change inside `knowledge-mcp`. The tool contract and the agents stay the same. A company with Confluence or Notion would point the `knowledge` capability at a different MCP server with the same tool names.
