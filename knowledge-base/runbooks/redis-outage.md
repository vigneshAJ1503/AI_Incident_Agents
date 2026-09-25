---
title: Redis cache outage
type: runbook
services: [payment-service, order-service, user-service, inventory-service]
tags: [redis, cache, econnrefused, fallback, latency, database-load]
alerts: [RedisDown, CacheHitRateLow, HighLatency]
owner: platform-sre
last_reviewed: 2026-09-01
---
# Redis cache outage

## Summary
Redis is a shared cache for sessions, idempotency keys and hot reads. The services are built to **fail open**: when Redis is unreachable they fall back to Postgres. So a Redis outage rarely causes hard errors at first. It shows up as **latency on every service at once**, plus more load on the database. If the outage lasts, the extra database load can exhaust connection pools and cause real errors.

## Symptoms
- Log pattern: `Redis connection refused: redis:6379 (ECONNREFUSED)` in several services at the same time.
- Log pattern: `Cache unavailable, fell back to database (POST /api/v1/pay ...)`.
- Latency rises on **all** services together, and the cache hit rate drops to ~0.
- Database queries per second and connection usage go up.
- `redis-cli -h redis ping` fails, or the Redis pod or container isn't running.
- Alerts: `RedisDown`, `CacheHitRateLow`, then `HighLatency` on several services.

## Known issues
- **Redis scaled to 0 during maintenance (INC-2026-061).** Redis was stopped for a node drain and not restarted. Every service logged `ECONNREFUSED` and fell back to the database, and latency rose everywhere. Fix: restart Redis; the caches warm up within about 5 minutes.

## Diagnosis
### 1. Confirm Redis is down or unreachable
- `kubectl get pods -n prod -l app=redis` (or `docker ps --filter name=redis` locally).
- `redis-cli -h <host> -p 6379 ping` should return `PONG`.
- Check the Redis logs for OOM, `MISCONF` persistence errors or maxclients.

### 2. Measure the blast radius
- Which services log `ECONNREFUSED` or cache fallbacks? If all of them do, Redis is the shared cause.
- Database load: connections and queries per second compared with the baseline. Is any pool close to exhaustion? (See [database-connection-pool](database-connection-pool.md).)

### 3. Network or Redis?
- If Redis is up but clients can't connect, check the NetworkPolicy, DNS (`nslookup redis`) and whether `maxclients` is reached.

## Mitigation
1. **Bring Redis back:** `kubectl scale statefulset/redis -n prod --replicas=1`, or restart the container.
2. Protect the database while the cache is cold: temporarily raise the pool sizes only if Postgres has headroom, or rate-limit expensive read endpoints.
3. If Redis can't be restored quickly, fail over to the replica (if configured) and point the clients at it.
4. After recovery, watch the cache hit rate recover and latency return to baseline (about 5 minutes).

## Rollback
- If a config or deployment change broke Redis (memory limit, persistence settings), revert it: `kubectl rollout undo statefulset/redis -n prod`.

## Escalation
- `#platform-oncall` owns Redis.
- If payment latency breaches the SLO (p95 > 1 s) for more than 15 minutes: SEV-2 in `#incidents`.

## Prevention
- Run Redis with a replica and Sentinel, or use a managed service with automatic failover.
- Alert on `redis_up == 0` and on the cache hit rate, not only on service latency.
- Load-test the database with a cold cache so the fallback path is sized.
