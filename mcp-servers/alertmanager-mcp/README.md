# alertmanager-mcp

A read-only, guard-railed Alertmanager (API v2) MCP server for AI Incident Agents (MASTER_PLAN D8, ADR-0005).

## Tools

| Tool | Purpose |
|------|---------|
| `list_alerts` | Alerts by `service` / `severity` / `alertname` / free `labels`, `state` = `active` \| `suppressed` \| `all`; most severe first, with counts by severity |
| `get_alert` | One alert by fingerprint |
| `list_silences` | Silences (`active` \| `pending` \| `expired` \| `all`) that could mute alerts with the given labels |
| `get_alert_groups` | Alerts grouped the way Alertmanager notifies (here by `service` + `alertname`) |
| `alert_history` | Alerts firing at some point in `[start, end]`. **Limited:** see below |

Each alert is returned as `fingerprint, alertname, service, severity, state, starts_at, summary, description, runbook_url, labels, silenced_by, inhibited_by, generator_url`. `endsAt` is left out on purpose: for an alert that hasn't ended, it's only an expiry time, and a model would read it as a resolution time.

### `alert_history` and Alertmanager's lack of history
Alertmanager's API returns only alerts that **haven't ended**. Resolved alerts are filtered out and then garbage-collected, and nothing is kept across restarts. Until Prometheus is connected, `alert_history` therefore returns the still-firing alerts that overlap the range. It says so in its result with `"complete": false` and a `note`. From PR-020, history comes from the Prometheus `ALERTS` / `ALERTS_FOR_STATE` series (through the metrics capability).

## Guardrails (enforced server-side, for every client)
- **Read-only:** the HTTP client exposes only `GET` endpoints. There are no tools to create silences or post alerts.
- **Label filters:** equality only (no regex). Label names must match `[a-zA-Z_][a-zA-Z0-9_]*`. Values must be non-empty, at most 256 characters and free of control characters, and quotes are escaped. At most `MAX_FILTERS` matchers per call, and a conflict between a shortcut argument and `labels` is rejected.
- **Result caps:** `limit` ≤ `MAX_RESULTS`, and responses report `total` / `truncated`.
- **Time range:** `alert_history` requires ISO-8601 `start`/`end` spanning at most `MAX_TIME_RANGE_HOURS`.
- **Timeouts:** `QUERY_TIMEOUT_S` per Alertmanager request.

## Configuration (env)

| Variable | Default |
|----------|---------|
| `AM_URL` | `http://localhost:9093` |
| `AM_BEARER_TOKEN` or `AM_USERNAME`/`AM_PASSWORD` | (none, for local) |
| `MAX_RESULTS` | `200` |
| `MAX_FILTERS` | `10` |
| `MAX_TIME_RANGE_HOURS` | `168` |
| `QUERY_TIMEOUT_S` | `15` |

## Run
```bash
uv run alertmanager-mcp --transport http --port 8105   # streamable HTTP at /mcp
uv run alertmanager-mcp --transport stdio
# or in Docker, next to the local stack (needs make alertmanager-up):
docker compose --env-file .env.example -f deploy/compose/docker-compose.mcp.yml up -d --build alertmanager-mcp
```

## Test
```bash
uv run pytest     # mocked Alertmanager HTTP API + in-process MCP client
```
