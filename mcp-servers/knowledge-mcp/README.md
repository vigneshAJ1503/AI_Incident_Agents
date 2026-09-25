# knowledge-mcp

A read-only MCP server that searches the team's runbooks and service docs with **Postgres full-text search** (MASTER_PLAN D5, UC-09, ADR-0004). It uses no embeddings and no models: `tsvector`, `ts_rank_cd` and `ts_headline` do the work.

The markdown in `knowledge-base/` is the source of truth. `aiops knowledge ingest` (`make ingest-knowledge`) chunks it by heading into the `knowledge` schema, and this server only reads that schema.

## Tools

| Tool | Purpose |
|------|---------|
| `search(query, k=5, services?, tags?, match="any", max_per_doc=3)` | Ranked sections: doc path, title, type, heading path, anchor, highlighted snippet, score, matched terms |
| `get_doc(path)` | The full markdown of one document, plus its section outline (heading paths and anchors) |
| `list_docs(doc_type?, service?)` | Indexed documents: path, title, type, services, tags |

### Ranking
Each chunk is one markdown section. Its `tsvector` is weighted **A** for the title and heading path, **B** for the body, and **C** for the front-matter tags, services and alert names.
- `match="any"` (default): the query's lexemes are OR-ed. Chunks are ordered by `0.7 × coverage + 0.3 × ts_rank_cd/(ts_rank_cd+1)`, where *coverage* is the fraction of query lexemes the chunk contains. Pasting a log line or a list of symptoms ranks the section that matches most of them first.
- `match="all"`: every word must match, using `websearch_to_tsquery` syntax (`"quoted phrase"`, `or`, `-exclude`).
- `services` keeps only documents tagged with one of those services. Generic runbooks (no services) are excluded by that filter, so search again without it to include them.

## Guardrails (enforced server-side, for every client)
- **Read-only:** each call opens a connection with `default_transaction_read_only=on` and runs in a `READ ONLY` transaction. In a shared database, also give the server a role that has only `SELECT` on the schema:
  ```sql
  CREATE ROLE knowledge_reader LOGIN PASSWORD '<secret>';
  GRANT USAGE ON SCHEMA knowledge TO knowledge_reader;
  GRANT SELECT ON ALL TABLES IN SCHEMA knowledge TO knowledge_reader;
  ```
- **Statement timeout:** `STATEMENT_TIMEOUT_MS`.
- **Caps:** `k` ≤ `MAX_K` (10), `max_per_doc` ≤ 5, query ≤ `MAX_QUERY_CHARS`, ≤ 10 filter values, `get_doc` content ≤ `MAX_DOC_CHARS` (with `truncated`), `list_docs` ≤ `MAX_LIST_DOCS`.
- **Input validation:** filters must be lowercase slugs; document paths must be relative `*.md` paths without `..`; the schema and text-search config names are validated identifiers.
- **Parameterized SQL only:** identifiers go through `psycopg.sql.Identifier`, and values are always bound parameters.

## Configuration (env)

| Variable | Default |
|----------|---------|
| `KNOWLEDGE_DATABASE_URL` | (empty: use libpq's `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`) |
| `KNOWLEDGE_SCHEMA` | `knowledge` |
| `KNOWLEDGE_TS_CONFIG` | `english` (must match the ingester) |
| `MAX_K` | `10` |
| `MAX_QUERY_CHARS` | `500` |
| `MAX_DOC_CHARS` | `60000` |
| `MAX_LIST_DOCS` | `500` |
| `STATEMENT_TIMEOUT_MS` | `5000` |

## Run
```bash
make infra-up ingest-knowledge
PGHOST=localhost PGPORT=15432 PGUSER=aiops PGPASSWORD=aiops-local-only PGDATABASE=aiops \
  uv run knowledge-mcp --transport http --port 8108      # streamable HTTP at /mcp
# or in Docker, next to the rest of the stack (127.0.0.1:8108):
docker compose --env-file .env.example -f deploy/compose/docker-compose.mcp.yml up -d --build knowledge-mcp
```

## Tests
```bash
uv run pytest                                  # unit tests: an in-memory repository, no Postgres
uv run pytest -m integration -o addopts=""     # against the live Postgres (after make ingest-knowledge)
```
