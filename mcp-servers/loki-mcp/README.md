# loki-mcp

A thin, read-only, guard-railed Grafana Loki MCP server for AI Incident Agents (PR-P4a,
ADR-0019). It backs the `logs` capability when a profile sets `capabilities.logs.provider: loki`
(`profiles/local-loki`, `docs/setup/loki.md`).

## Tools

| Tool | Purpose |
|------|---------|
| `query_range` | LogQL over `[start, end]`: **log lines** for a log query (`{...} \|= "x" \| json`), newest or oldest first, capped by `limit`; **series** for a metric query (step auto-chosen when omitted) |
| `query` | Instant **metric** LogQL (`count_over_time`, `sum by`, `topk`, `min_over_time(... \| unwrap ...)`) at `time` (default now) |
| `list_labels` | Stream label names in a time range (scoped to the allowlisted streams) |
| `label_values` | Values of one stream label (scoped to the allowlisted streams or a given allowlisted selector) |

Results are compact and deterministic: log lines as `{timestamp (ISO, ms), labels, line}`
sorted by time, series sorted by labels, whole numbers kept exact (counts, epoch
milliseconds), `truncated` when a cap applies.

## Guardrails (enforced server-side, before Loki is contacted)
- **Read-only:** only `GET` requests to the query and label endpoints. No push, delete,
  rules or admin API.
- **Stream allowlist:** every stream selector `{...}` in a query must contain an exact
  matcher from `ALLOWED_STREAMS` (default `namespace=prod`), e.g. `{namespace="prod", app="x"}`.
  `{app=~".+"}` or `{namespace=~"prod|kube-system"}` are rejected. Label listings are scoped
  to the same streams. The scanner understands `"..."` and `` `...` `` strings, so braces in
  regexes or `line_format` templates can't hide a selector.
- **Bounded time:** `query_range` spans at most `MAX_RANGE_HOURS`; every `[range]` and
  `offset` in a query is held to the same bound.
- **Bounded size:** `MAX_LINES` log lines, `MAX_SERIES` series, `MAX_POINTS` points per
  series (metric `query_range`), `MAX_RESULTS` label names/values, `MAX_QUERY_LENGTH`
  characters; no control characters.
- **Timeouts:** `QUERY_TIMEOUT_S` bounds every HTTP call; Loki's own `limits_config`
  (`query_timeout`, `max_query_series`, `max_entries_limit_per_query`) stays the last line of
  defence.

## Configuration (env)

| Variable | Default |
|----------|---------|
| `LOKI_URL` | `http://localhost:3100` |
| `LOKI_BEARER_TOKEN` or `LOKI_USERNAME`/`LOKI_PASSWORD` (Grafana Cloud: instance id + token) | (none, for local) |
| `LOKI_ORG_ID` (sent as `X-Scope-OrgID`: multi-tenant Loki) | (none) |
| `ALLOWED_STREAMS` | `namespace=prod` (comma-separated `label=value`) |
| `QUERY_TIMEOUT_S` | `30` |
| `MAX_RANGE_HOURS` | `48` |
| `MAX_LINES` | `500` |
| `MAX_SERIES` | `1000` |
| `MAX_POINTS` | `1100` |
| `MIN_STEP_S` | `1` |
| `MAX_QUERY_LENGTH` | `4000` |
| `MAX_RESULTS` | `500` |

## Run

```bash
make loki-up                                  # Loki (127.0.0.1:3101) + loki-mcp (127.0.0.1:8110)
uv run loki-mcp --transport http --port 8110  # or locally, LOKI_URL=http://localhost:3101
```

Docker: non-root (UID 10001), read-only root filesystem, 128 MB limit.

## Development

```bash
uv sync && uv run ruff check . && uv run mypy && uv run pytest
```
