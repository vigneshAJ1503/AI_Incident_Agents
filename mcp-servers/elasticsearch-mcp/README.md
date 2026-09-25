# elasticsearch-mcp

A read-only, guard-railed Elasticsearch MCP server for AI Incident Agents (ADR-0003).

## Tools

| Tool | Purpose |
|------|---------|
| `list_indices` | Allowed indices, with doc counts and sizes |
| `get_mapping` | Field names and types for an index pattern |
| `search_logs` | Sample log documents (Lucene query, levels, sort, fields) |
| `execute_esql` | Read-only ES\|QL, automatically filtered to `[start, end]` |

## Guardrails (enforced server-side, for every client)
- **Index allowlist:** `ALLOWED_INDEX_PATTERNS`. `*`, `_all`, system (`.`) and exclusion (`-`) patterns are rejected.
- **Time range:** `start`/`end` are required; the range can't exceed `MAX_TIME_RANGE_HOURS`.
- **Result size:** capped at `MAX_RESULTS`.
- **Query timeout:** `QUERY_TIMEOUT_S`.
- **ES|QL:** must start with `FROM <allowed pattern>`; `ENRICH` / `LOOKUP JOIN` are rejected.
- **Read-only:** no write, delete or admin endpoints exist in this server. Also give it read-only ES credentials.

## Configuration (env)

| Variable | Default |
|----------|---------|
| `ES_URL` | `http://localhost:9200` |
| `ES_API_KEY` or `ES_USERNAME`/`ES_PASSWORD` | (none, for local) |
| `ALLOWED_INDEX_PATTERNS` | the sample services' `*-prod-*` / `*-staging-*` patterns and `logs-k8s-*` |
| `MAX_TIME_RANGE_HOURS` | `48` |
| `MAX_RESULTS` | `1000` |
| `QUERY_TIMEOUT_S` | `30` |
| `TIMESTAMP_FIELD` | `@timestamp` |

## Run
```bash
uv run elasticsearch-mcp --transport http --port 8101   # streamable HTTP at /mcp
uv run elasticsearch-mcp --transport stdio
# or with the rest of the stack:
make mcp-up
```
