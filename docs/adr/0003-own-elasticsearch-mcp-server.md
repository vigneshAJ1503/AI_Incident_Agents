# ADR-0003: Our own read-only Elasticsearch MCP server

- **Status:** Accepted
- **Date:** 2026-09-25

## Context
The Log agent needs log search and aggregation through MCP. The options were Elastic's official MCP server or a thin server of our own (FastMCP-style, MCP SDK v2 `MCPServer`).

## Decision
Build `mcp-servers/elasticsearch-mcp` with exactly four read-only tools: `list_indices`, `get_mapping`, `search_logs` and `execute_esql`. The guardrails are enforced **server-side**:
- index allowlist
- maximum time range, with `start`/`end` required and ES|QL filtered by them
- result cap and query timeout
- ES|QL scope (`FROM` only; no `ENRICH` / `LOOKUP JOIN`)

## Consequences
- Any client, not only our agents, gets the same protection. This is defense in depth on top of the client-side allowlist from PR-005.
- There's no dependency on a vendor server's release cycle, license or tool naming, and it works with Elasticsearch and (with small changes) OpenSearch.
- Tool names are stable, so the recorded fixtures and prompts stay valid.
- We maintain ~300 lines of code. If a company already runs an official Elastic MCP server, the capability config can point at it (with a different allowlist and prompt version) with no agent code changes.
