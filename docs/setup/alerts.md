# Alerts locally: Alertmanager, rules and seeded alerts

The alerts slice follows the same path as logs: **synthetic first, real later, config-only switch**.

| Piece | Where | Status |
|-------|-------|--------|
| Alertmanager `v0.34.1` on `127.0.0.1:9093` | `deploy/compose/docker-compose.infra.yml`, config in `deploy/compose/config/alertmanager/alertmanager.yml` | PR-023 |
| Alert rules (`HighErrorRate`, `HighLatencyP95`, `DatabaseConnectionPoolExhausted`, `PodCrashLooping`, `PodOOMKilled`, `DeploymentReplicasMismatch`, `RedisDown`) | `deploy/compose/config/prometheus/alert-rules.yml` (+ promtool unit tests in `alert-rules.test.yml`) | authored in PR-023, **evaluated from PR-020** |
| Seeded alerts per scenario | `aiops seed alerts` → Alertmanager API v2 | PR-023, until PR-020 |

```bash
make alertmanager-up          # or make infra-up (starts everything)
make seed-alerts S=S1         # S0 clears all seeded alerts
open http://localhost:9093    # Alertmanager UI
make check-rules              # promtool check + test, amtool check-config (runs in CI too)
```

## What each scenario fires

The timeline matches the synthetic logs: the incident starts at **now-20m** (S1's rollout at now-22m).

| Scenario | Alerts (service, severity, start) |
|----------|-----------------------------------|
| S0 | none |
| S1 | `DatabaseConnectionPoolExhausted` (payment-service, critical, +1m), `HighErrorRate` (payment-service, critical, +2m) |
| S2 | `HighErrorRate` (+2m), `PodOOMKilled` (+3m), `PodCrashLooping` (+12m), all order-service, critical |
| S3 | `HighErrorRate` order-service (critical, +3m), `HighLatencyP95` inventory-service (warning, +5m), `HighLatencyP95` order-service (warning, +6m) |
| S4 | `DeploymentReplicasMismatch` user-service (warning, +5m) |
| S5 | `RedisDown` redis (critical, +1m), `HighLatencyP95` payment-service (+6m) and order-service (+7m), warnings |

Seeded alerts carry the same alert names, labels (`service`, `severity`, `team`, `namespace`) and annotations (`summary`, `description`, `runbook_url`) as the rules. A unit test keeps the two in sync.

## How seeded alerts stay alive (and go away)

Alertmanager has no "firing forever": every alert has an `endsAt`, and it resolves the alert at that time.

- **Prometheus** re-sends each firing alert on every evaluation with `endsAt` a few minutes ahead. If it stops sending, the alert resolves by itself.
- **Alerts posted without `endsAt`** resolve after `resolve_timeout` (5m here).
- **The seeder** runs once, with no refresh loop, so it sets `endsAt = wall clock + --ttl-hours` (24h by default). `startsAt` follows the scenario anchor (`--now`), so alerts seeded at the fixed fixture time `2026-09-25T10:30Z` still show the right "firing since".
- **Re-seeding** first resolves the previous seed's alerts, which it finds by their `generatorURL` (`aiops-seed://<scenario>/<alert>`). It resolves them with `endsAt == startsAt`: Alertmanager merges a new alert into a stored alert with the same labels when their time ranges overlap, and an explicit resolve wins that merge. A zero-length range never overlaps, so a re-seeded alert always fires again.
- **Restarting Alertmanager** drops all alerts, because it keeps them only in memory. Silences survive in the `am-data` volume. Run `make seed-alerts` again after a restart.

## Limits to know

- **No history in Alertmanager.** `GET /api/v2/alerts` returns only alerts that haven't ended, and resolved alerts are garbage-collected. Real alert history comes from Prometheus's `ALERTS` / `ALERTS_FOR_STATE` series once PR-020 lands. `alertmanager-mcp`'s `alert_history` states this in its output.
- **Runbook links** point to `knowledge-base/runbooks/*.md`, which the Knowledge slice (PR-028) writes.
- **Switch to real alerts in PR-020:** load `alert-rules.yml` in Prometheus and point it at this Alertmanager. The agent and MCP server don't change.
