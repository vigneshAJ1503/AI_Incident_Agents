# alertmanager-mcp

A read-only, guard-railed Alertmanager (API v2) MCP server for AI Incident Agents (MASTER_PLAN D8, ADR-0005).

## Tools

| Tool | Purpose |
|------|---------|
| `list_alerts` | Alerts by `service` / `severity` / `alertname` / free `labels`, `state` = `active` \| `suppressed` \| `all`; most severe first, with counts by severity |
| `get_alert` | One alert by fingerprint |
| `list_silences` | Silences (`active` \| `pending` \| `expired` \| `all`) that could mute alerts with the given labels |
| `get_alert_groups` | Alerts grouped the way Alertmanager notifies (here by `service` + `alertname`) |
| `alert_history` | Alerts firing at some point in `[start, end]`, resolved ones included when Prometheus is configured (see below) |

Each alert is returned as `fingerprint, alertname, service, severity, state, starts_at, summary, description, runbook_url, labels, silenced_by, inhibited_by, generator_url`. `endsAt` is left out on purpose: for an alert that hasn't ended, it's only an expiry time, and a model would read it as a resolution time.

### `alert_history` and Alertmanager's lack of history
Alertmanager's API returns only alerts that **haven't ended**. Resolved alerts are filtered out and then garbage-collected, and nothing is kept across restarts. So:

- **With `PROMETHEUS_URL` set** (the local compose default, PR-020): `alert_history` range-queries Prometheus's `ALERTS{alertstate="firing", <filters>}` series and returns one entry per firing interval: `firing_since`, `resolved_at` (null while still firing), `started_before_range`, `state` (`firing` | `resolved`), labels, plus summary/runbook joined from Alertmanager by identical labels. Alerts that Alertmanager holds but Prometheus never evaluated (seeded alerts, other sources) are appended with `"source": "alertmanager"`. The result says `"complete": true`, `"sources": ["prometheus", "alertmanager"]` and the `step_s` (times are accurate to one step; the step grows so a series never exceeds 1000 points).
- **Without it, or if Prometheus fails:** the still-firing alerts that overlap the range, with `"complete": false` and a `note` saying why.

## Guardrails (enforced server-side, for every client)
- **Read-only:** the HTTP clients (Alertmanager, and Prometheus for history) only send `GET` requests. There are no tools to create silences or post alerts.
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
| `PROMETHEUS_URL` | (none) — e.g. `http://aiops-prometheus:9090`; enables full `alert_history` |
| `HISTORY_STEP_S` | `30` (minimum step; keep ≥ the rule evaluation interval) |

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
