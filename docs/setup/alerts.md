# Alerts locally: Alertmanager, rules and seeded alerts

The alerts slice follows the same path as logs: **synthetic first, real later, config-only switch**. Since PR-020 the real part exists: Prometheus evaluates the rules against the live cluster ([metrics.md](metrics.md)).

| Piece | Where | Status |
|-------|-------|--------|
| Alertmanager `v0.34.1` on `127.0.0.1:9093` | `deploy/compose/docker-compose.infra.yml`, config in `deploy/compose/config/alertmanager/alertmanager.yml` | PR-023 |
| Alert rules (`HighErrorRate`, `HighLatencyP95`, `DatabaseConnectionPoolExhausted`, `PodCrashLooping`, `PodOOMKilled`, `DeploymentReplicasMismatch`, `RedisDown`) | `deploy/compose/config/prometheus/alert-rules.yml` (+ promtool unit tests in `alert-rules.test.yml`) | authored in PR-023, **evaluated by Prometheus since PR-020** |
| Seeded alerts per scenario | `aiops seed alerts` → Alertmanager API v2 | PR-023; still used for fixed-time evals (below) |

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

- **No history in Alertmanager.** `GET /api/v2/alerts` returns only alerts that haven't ended, and resolved alerts are garbage-collected. Since PR-020, `alertmanager-mcp`'s `alert_history` reads Prometheus's `ALERTS` series instead (`PROMETHEUS_URL`, set in `docker-compose.mcp.yml`), so resolved real alerts are included (`"complete": true`). Without Prometheus it falls back to Alertmanager and says so (`"complete": false`).
- **Runbook links** point to `knowledge-base/runbooks/*.md`, which the Knowledge slice (PR-028) writes.

## Real alerts and seeded alerts

Both end up in the same Alertmanager, and the Alert agent reads them the same way. They differ in where they come from:

| | Real alerts | Seeded alerts |
|--|-------------|---------------|
| Source | Prometheus rule evaluation on live metrics (`make inject-fault S=S1`, or any real problem) | `make seed-alerts S=S1` / `aiops seed alerts` |
| `generatorURL` | `http://localhost:9090/graph?...` | `aiops-seed://<scenario>/<alert>` |
| Timeline | whenever the fault really happened | anchored to the synthetic log timeline (incident at now-20m) |
| Lifetime | re-sent every 30 s while firing; resolves ~1 evaluation after the condition clears | `endsAt` = wall clock + 24h, or until re-seeded / `S=S0` |
| In `alert_history` | from Prometheus `ALERTS`, resolved ones included | from Alertmanager only (Prometheus never saw them) |

Rules of thumb:

- **Don't mix them in one run.** An alert is identified by its labels, and seeded alerts use exactly the labels of the real rules. A seeded `HighErrorRate{service="payment-service",namespace="prod"}` and a real one merge into a single Alertmanager alert. Run `make seed-alerts S=S0` (clears only seeded alerts, found by `generatorURL`) before investigating a live fault, and don't seed while `make fault-status` shows an active scenario.
- **Is seeding still needed?** Not for live investigations: real faults now fire real alerts. It stays for what needs a *fixed, reproducible* timeline: the Alert agent's recorded fixtures (`make record-fixtures-alerts`), its `MODE=live` eval (seeded alongside the synthetic logs, so both agents see the same incident), and demos without Minikube. Re-recording the Alert agent's fixtures from live faults (`aiops fault run SX -- ...`, like the Metrics agent's) is a follow-up; the seeded fixtures stay valid because the alert names and labels are identical.
- A few minutes after a live fault is reverted, its alerts are still firing (the rules use 5-minute rate windows) and then resolve by themselves. Leave ~5 minutes between live scenarios if you look at alerts.
