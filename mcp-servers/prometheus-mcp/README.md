# prometheus-mcp

A thin, read-only, guard-railed Prometheus (HTTP API v1) MCP server for AI Incident Agents (PR-021, ADR-0009). It backs the `metrics` capability.

## Tools

| Tool | Purpose |
|------|---------|
| `query` | Instant PromQL query at `time` (default now) |
| `query_range` | PromQL over `[start, end]` at `step` (auto-chosen when omitted); values are `[unix_seconds, value]` |
| `list_metrics` | Metric names, filtered by a substring (allowlisted names only) |
| `metric_metadata` | Type, help and unit of one or all metrics |
| `get_targets` | Scrape targets, health and last error ("is the data even collected?") |

Results are compact and deterministic: series sorted by labels, values rounded to 6 significant digits, NaN/Inf returned as `null`, and `series_total` / `returned` / `truncated` when the series cap applies.

## Guardrails (enforced server-side, before Prometheus is contacted)
- **Read-only:** only `GET` requests to query/metadata endpoints. There is no admin API access (no delete series, snapshots or reloads).
- **Bounded time:** `query_range` spans at most `MAX_RANGE_HOURS`. Every `[range]` selector, subquery and `offset` in a query is held to the same bound.
- **Bounded resolution:** step ≥ `MIN_STEP_S`, and at most `MAX_POINTS` points per series. When `step` is omitted, the finest step that fits is chosen.
- **Bounded size:** at most `MAX_SERIES` series per result, `MAX_RESULTS` metric names/targets per listing, and `MAX_QUERY_LENGTH` characters per query.
- **No whole-TSDB scans:** every selector must name its metric. Bare `{job=~".+"}` selectors and `__name__` matchers are rejected.
- **Optional metric allowlist:** `METRIC_ALLOWLIST` (comma-separated regexes, full match). Queries that touch other metrics are rejected, and listings hide them.
- **Timeouts:** `QUERY_TIMEOUT_S` is sent to Prometheus as `timeout=` (it aborts the evaluation) and bounds the HTTP call. Prometheus's own `--query.max-samples` stays the last line of defence.

## Configuration (env)

| Variable | Default |
|----------|---------|
| `PROM_URL` | `http://localhost:9090` |
| `PROM_BEARER_TOKEN` or `PROM_USERNAME`/`PROM_PASSWORD` | (none, for local) |
| `QUERY_TIMEOUT_S` | `20` |
| `MAX_RANGE_HOURS` | `24` |
| `MAX_POINTS` | `1100` |
| `MIN_STEP_S` | `15` |
| `MAX_SERIES` | `50` |
| `MAX_QUERY_LENGTH` | `2000` |
| `MAX_RESULTS` | `500` |
| `METRIC_ALLOWLIST` | (empty = all metrics) |

## Run
```bash
uv run prometheus-mcp --transport http --port 8103   # streamable HTTP at /mcp
uv run prometheus-mcp --transport stdio
# or in Docker, next to the local stack (needs make infra-up):
make prometheus-mcp-up
```

## Test
```bash
uv run pytest     # mocked Prometheus HTTP API + in-process MCP client
```
