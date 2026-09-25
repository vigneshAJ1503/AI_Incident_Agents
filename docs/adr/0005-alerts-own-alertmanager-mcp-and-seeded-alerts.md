# ADR-0005: Our own Alertmanager MCP server, seeded alerts before Prometheus

- **Status:** Accepted
- **Date:** 2026-09-25

## Context
The Alert agent (UC-06) needs firing alerts, silences and alert groups for a service. Community Alertmanager MCP servers don't cleanly cover this (MASTER_PLAN D8), and they don't enforce guardrails on the server. Prometheus and Minikube arrive later (PR-015/PR-020), but the alerts slice (PR-023–025) is built now.

## Decision
1. Build `mcp-servers/alertmanager-mcp` (MCP SDK v2 `MCPServer`, like ADR-0003) with five read-only tools: `list_alerts`, `get_alert`, `list_silences`, `get_alert_groups` and `alert_history`. It has server-side guardrails: GET-only client, equality-only validated label filters, result caps, a bounded time range and timeouts.
2. **Seed alerts synthetically** into a real Alertmanager through `POST /api/v2/alerts` (`aiops seed alerts --scenario Sx`). The seeded alerts have the same names, labels and annotations as the Prometheus rules in `deploy/compose/config/prometheus/alert-rules.yml`, which PR-020 will load; a unit test keeps the two in sync. This mirrors D12 for logs: synthetic first, real later, config-only switch.
3. Seeded alerts get `endsAt = wall clock + TTL`. Re-seeding resolves the previous seed with a zero-length range, so Alertmanager's overlap merge can't keep a re-seeded alert resolved.
4. **History:** Alertmanager's API returns only alerts that haven't ended. `alert_history` returns what it can and marks the result `complete: false`, with a note. Real history comes from Prometheus `ALERTS` in PR-020.

## Consequences
- The Alert agent and its fixtures are built on a real Alertmanager API today. PR-020 only adds Prometheus as the alert source; the agent, the MCP server and the capability config don't change.
- Resolved alerts can't be shown before PR-020. The agent says so rather than implying there were no earlier alerts.
- Alertmanager keeps alerts in memory, so a restart needs `make seed-alerts` again. Silences persist in a volume.
- We maintain ~350 lines of server code.
