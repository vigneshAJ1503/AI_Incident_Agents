---
title: High latency (p95/p99)
type: runbook
services: []
tags: [latency, p95, p99, slow, slo, saturation, triage]
alerts: [HighLatency, LatencySLOBurn]
owner: platform-sre
last_reviewed: 2026-09-01
---
# High latency (p95/p99)

## Summary
This is a generic triage runbook for a service whose p95 or p99 latency is above its SLO. Latency comes from one of three places: **waiting** (queues, pools, locks), **working** (CPU, GC, slow queries) or **calling others** (dependencies, cache, database). Find out which one before you change anything.

## Symptoms
- Alert `HighLatency`: `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m]))) > 1`.
- Users report that pages or payments are slow; there may be no errors.
- Callers start timing out (HTTP 504) if latency passes their client timeout.

## Diagnosis
### 1. One service or all of them?
- **All services slow at once:** suspect shared infrastructure, meaning the cache ([redis-outage](redis-outage.md)), the database or the node or network.
- **One service:** continue below.

### 2. Waiting, working or calling?
- **Waiting:** pool saturation (`db_pool_pending > 0`), thread pool queues, `Request queue depth high`. See [database-connection-pool](database-connection-pool.md), or capacity in [bad-deployment-rollback](bad-deployment-rollback.md).
- **Working:** CPU throttling (`container_cpu_cfs_throttled_seconds_total`), GC pauses ([memory-leak-oom](memory-leak-oom.md)), slow queries ([slow-database-queries](slow-database-queries.md)).
- **Calling others:** per-dependency client latency. Look for `Timeout calling` logs ([dependency-timeouts](dependency-timeouts.md)).

### 3. What changed?
- Deployments, traffic compared with the baseline, config and feature flags, and infrastructure events in the 30 minutes before the latency rose.

## Mitigation
1. Roll back if a release correlates with the start time.
2. Scale out if the service is CPU-bound or queue-bound and the dependencies have headroom.
3. Shed or defer non-critical work (batch jobs, reports).
4. Enable degraded modes (cached responses, async processing).

## Escalation
- The owning team's on-call; `#platform-oncall` for shared infrastructure (cache, database, nodes).
- SEV-2 if the latency SLO is breached for more than 30 minutes on a customer-facing path.
