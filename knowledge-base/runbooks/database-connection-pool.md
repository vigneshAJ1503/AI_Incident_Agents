---
title: Database connection pool exhaustion
type: runbook
services: [payment-service, order-service, user-service, inventory-service]
tags: [database, postgres, connection-pool, hikari, timeouts, http-500]
alerts: [DatabaseConnectionPoolExhausted, HighErrorRate]
owner: platform-sre
last_reviewed: 2026-09-01
---
# Database connection pool exhaustion

## Summary
Every service talks to Postgres through a fixed-size connection pool (HikariCP in the JVM services, `DB_POOL_SIZE`). When all connections are busy, new requests wait for a free connection. After `connectionTimeout` (5 s by default) the wait fails, the request returns **HTTP 500**, and latency climbs for every request that touches the database.

The pool runs out for one of three reasons:
- it is **too small** for the traffic, usually after a config change or a new release;
- connections are **held too long**, because of slow queries, long transactions or a slow dependency called inside a transaction;
- connections **leak**, because code paths never return them to the pool.

## Symptoms
- Log pattern: `Database connection timeout: could not acquire a connection from the pool within 5000ms (pool size=N, active=N, waiting=M)`.
- Log pattern: `HikariPool-1 - Connection is not available, request timed out after 5000ms`.
- HTTP 500 on endpoints that write to the database (for payment-service, `POST /api/v1/pay`).
- p95 latency close to the pool timeout (≈5 s), while CPU stays low.
- Metrics: `db_pool_active == db_pool_max` (saturation at 100%) and a growing `db_pool_pending`.
- Alerts: `DatabaseConnectionPoolExhausted`, followed by `HighErrorRate`.

## Known issues
- **Pool size lowered by a release (INC-2026-031).** payment-service `v1.8.2` shipped `DB_POOL_SIZE=2` (it was 20) in a commit titled "tune db pool". Within minutes of the rollout, requests queued for a connection, `waiting` climbed above 15 and `POST /api/v1/pay` returned HTTP 500. Fix: roll back to `v1.8.1`, then restore `DB_POOL_SIZE=20` in the ConfigMap.
- **Long transaction around a remote call.** order-service once called inventory-service inside an open transaction. A slow inventory-service held every connection. See [dependency-timeouts](dependency-timeouts.md).

## Diagnosis
### 1. Confirm the pool is the bottleneck
- Search the logs for `could not acquire a connection` or `Connection is not available`. Note the first-seen time and the `pool size` / `waiting` values in the message.
- Check pool saturation: `max_over_time(db_pool_active{service="payment-service"}[5m]) / db_pool_max`. A ratio of 1.0 means the pool is exhausted.

### 2. Check what changed
- Compare the first-seen time of the timeouts with the latest rollout (`kubectl rollout history deployment/payment-service -n prod`).
- Diff the pool settings between the running and the previous version: `DB_POOL_SIZE`, `DB_POOL_TIMEOUT_MS`, `maximumPoolSize`.
- Look for commits touching `config/<service>/` or the datasource configuration.

### 3. Rule out the database itself
- On Postgres, count connections per application: `SELECT application_name, state, count(*) FROM pg_stat_activity GROUP BY 1, 2;`
- Look for long-running queries or lock waits: `SELECT pid, now() - query_start AS age, wait_event_type, query FROM pg_stat_activity WHERE state <> 'idle' ORDER BY age DESC LIMIT 10;`
- If Postgres is near `max_connections` or shows lock waits, the pool isn't the root cause. Go to [slow-database-queries](slow-database-queries.md).

## Mitigation
1. **If a release changed the pool settings, roll back first.** See [bad-deployment-rollback](bad-deployment-rollback.md): `kubectl rollout undo deployment/payment-service -n prod`.
2. If no release is involved, raise the pool size temporarily (ConfigMap `DB_POOL_SIZE`, then `kubectl rollout restart`). Keep `replicas × DB_POOL_SIZE` below Postgres `max_connections` minus a 20% reserve.
3. If connections are held by slow queries, kill the worst offenders (`SELECT pg_cancel_backend(pid)`) and follow [slow-database-queries](slow-database-queries.md).
4. Shed load if needed: lower the ingress rate limit for the affected endpoint.

## Rollback
- `kubectl rollout undo deployment/<service> -n prod` restores the previous ReplicaSet, including its ConfigMap reference.
- Verify with `kubectl rollout status` and watch the timeout pattern disappear within 2–3 minutes.
- Revert the offending config commit in git so the next deploy doesn't reintroduce it.

## Escalation
- First: the owning team's on-call (see the service doc, e.g. [payment-service](../services/payment-service.md)).
- If Postgres itself is saturated: `#dba-oncall`.
- Customer-facing payment failures for more than 15 minutes: declare a SEV-2 in `#incidents`.

## Prevention
- Alert on pool saturation (`db_pool_active / db_pool_max > 0.9` for 5 minutes) before errors start.
- Require review from the owning team for any change to `DB_POOL_*` settings.
- Load-test pool settings in staging with production-like concurrency.
