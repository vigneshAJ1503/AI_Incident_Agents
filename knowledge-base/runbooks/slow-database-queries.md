---
title: Slow database queries
type: runbook
services: [inventory-service, order-service, payment-service, user-service]
tags: [database, postgres, slow-query, index, locks, latency]
alerts: [SlowQueries, HighLatency]
owner: dba
last_reviewed: 2026-09-01
---
# Slow database queries

## Summary
A query that used to take milliseconds now takes seconds. This happens because of a missing or unused index, a bad query plan after statistics changed, lock contention, or table bloat. Slow queries hold connections longer, which can exhaust the connection pool ([database-connection-pool](database-connection-pool.md)) and make callers time out ([dependency-timeouts](dependency-timeouts.md)).

## Symptoms
- Log pattern: `Slow query on stock_levels took 4200ms`.
- The service's latency rises, while the error rate may stay low at first.
- Callers log `Timeout calling <service>`.
- Postgres: `pg_stat_activity` shows many active queries with a large `now() - query_start`.
- Alerts: `SlowQueries`, `HighLatency`.

## Diagnosis
### 1. Find the slow statements
- `SELECT query, calls, mean_exec_time, max_exec_time FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 10;`
- Live view: `SELECT pid, now() - query_start AS age, wait_event_type, wait_event, left(query, 120) FROM pg_stat_activity WHERE state = 'active' ORDER BY age DESC;`

### 2. Why is it slow?
- `EXPLAIN (ANALYZE, BUFFERS) <query>` on a replica: look for `Seq Scan` on a large table, or row estimates that are far off.
- Locks: `SELECT * FROM pg_locks WHERE NOT granted;`, then find the blocker with `pg_blocking_pids(pid)`.
- A recent migration or deploy that changed the query or dropped an index?

## Mitigation
1. Cancel runaway queries: `SELECT pg_cancel_backend(pid);` (use `pg_terminate_backend` only if cancelling doesn't work).
2. Add the missing index concurrently: `CREATE INDEX CONCURRENTLY ...`.
3. Run `ANALYZE <table>` if the statistics are stale.
4. Serve cached data or reduce the query frequency in the calling service.

## Rollback
- If a migration or release introduced the query, roll back the release. Drop a harmful index only after the rollback.

## Escalation
- `#dba-oncall`, together with the owning service team.
