---
title: High error rate (HTTP 5xx)
type: runbook
services: []
tags: [errors, http-500, http-5xx, error-rate, slo, triage]
alerts: [HighErrorRate, ErrorBudgetBurn]
owner: platform-sre
last_reviewed: 2026-09-01
---
# High error rate (HTTP 5xx)

## Summary
This is a generic triage runbook for a service whose 5xx rate is above its SLO (for example above 1% for 5 minutes). A high error rate is a symptom, not a cause. The goal is to find out quickly **which** errors are new, **when** they started and **what** changed at that time, and then move to the specific runbook.

## Symptoms
- Alert `HighErrorRate`: `sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m])) > 0.01`.
- An `ErrorBudgetBurn` page for a fast budget burn.
- Users report failed requests, and callers log HTTP 500 / 502 / 503 / 504 from this service.

## Diagnosis
### 1. What kind of errors?
- Group the error logs by message pattern, endpoint and status code. A **new** pattern is the strongest lead; patterns that also exist in the 24 h baseline are usually noise.
- Status codes point in a direction:
  - **500**: the application threw an error (database, bug or config).
  - **502/503**: nothing healthy is serving (pods not ready, crash loops, capacity).
  - **504**: a slow dependency or a timeout ([dependency-timeouts](dependency-timeouts.md)).

### 2. When did it start, and what changed?
- The first-seen time of the new pattern, compared with deployments, config changes, feature flags, traffic spikes and infrastructure events.
- A release in the previous 30 minutes: see [bad-deployment-rollback](bad-deployment-rollback.md).

### 3. Map the pattern to a specific runbook
| Pattern or signal | Runbook |
|-------------------|---------|
| `could not acquire a connection`, `HikariPool` | [database-connection-pool](database-connection-pool.md) |
| `OutOfMemoryError`, OOMKilled, restarts | [memory-leak-oom](memory-leak-oom.md) |
| `Timeout calling <service>` | [dependency-timeouts](dependency-timeouts.md) |
| `ECONNREFUSED` to Redis, cache fallback | [redis-outage](redis-outage.md) |
| CrashLoopBackOff | [pod-crashloop](pod-crashloop.md) |
| ready replicas below desired, ImagePullBackOff | [bad-deployment-rollback](bad-deployment-rollback.md) |

## Mitigation
1. If a change correlates with the start time, roll it back.
2. If one endpoint dominates, consider disabling it with a feature flag or rate-limiting it.
3. Communicate: post the impact and ETA in `#incidents` if the error rate stays above 5% for more than 10 minutes.

## Escalation
- The owning team's on-call (service docs in `knowledge-base/services/`).
- SEV-2 if customer-facing errors stay above 5% for more than 15 minutes; SEV-1 if payments or login are fully down.
