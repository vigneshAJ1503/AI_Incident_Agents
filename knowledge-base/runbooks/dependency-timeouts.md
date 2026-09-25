---
title: Downstream dependency timeouts
type: runbook
services: [order-service, payment-service, user-service, inventory-service]
tags: [dependency, downstream, timeouts, latency, http-504, circuit-breaker, retries]
alerts: [DependencyTimeouts, HighLatency, HighErrorRate]
owner: platform-sre
last_reviewed: 2026-09-01
---
# Downstream dependency timeouts

## Summary
A service calls another service (a *downstream dependency*) with a client timeout. When the dependency becomes slow, the caller waits until its timeout expires and then fails the request, usually with **HTTP 504** (gateway timeout) or 500. The errors show up in the **caller**, but the cause is in the **callee**. Always follow the chain downstream before changing the caller.

Retries make it worse: every retry adds load to a dependency that is already slow.

## Symptoms
- Log pattern in the caller: `Timeout calling inventory-service GET /api/v1/stock/{id} after 3000ms`.
- Caller latency is capped close to its client timeout (for example p95 ≈ 3 s) and HTTP 504s appear.
- The dependency's own logs show slow requests or slow queries (for example `Slow query on stock_levels took 4200ms`), often without errors.
- Thread or connection pools in the caller fill up with requests that are waiting.
- Alerts: `DependencyTimeouts` on the caller, `HighLatency` on the dependency.

## Known issues
- **inventory-service slow `stock_levels` query (INC-2026-052).** A missing index on `stock_levels(sku, warehouse_id)` made stock lookups take 3–5 s. order-service calls to inventory-service time out after 3000 ms, so orders fail with HTTP 504. Fix: add the index; mitigate by serving cached stock levels.

## Diagnosis
### 1. Identify the slow dependency
- Group the caller's timeout errors by target: the log message names the dependency and the endpoint (`Timeout calling <service> <method> <path>`).
- Check the dependency's latency (p95/p99) and error rate over the same window. If its latency rose first, it is the cause.

### 2. Look inside the dependency
- Look for slow queries (`Slow query`), lock waits, GC pauses, CPU throttling or a recent deployment in the dependency.
- Check the dependency's own dependencies (database, cache). Timeouts cascade.
- Compare the dependency's traffic with the baseline. A retry storm from callers shows up as a traffic jump.

### 3. Check the caller's settings
- Client timeout, retry count and backoff. `retries × timeout` must be lower than the caller's own SLA.
- Is there a circuit breaker, and did it open?

## Mitigation
1. Fix or scale the **dependency** first (see [slow-database-queries](slow-database-queries.md) if queries are slow; roll back if a release caused it).
2. Reduce the pressure: disable retries or lower them to 1 in the caller, and open the circuit breaker so the caller fails fast.
3. Degrade gracefully: serve cached or default data (for example the last known stock level), or queue the work and confirm the order asynchronously.
4. Don't raise the caller's timeout as a fix. It only moves the queue into the caller.

## Rollback
- If a release of the dependency made it slow: `kubectl rollout undo deployment/<dependency> -n prod`.
- If the caller changed its timeout or retry settings recently, revert that config.

## Escalation
- The **dependency's** owning team (see its service doc, for example [inventory-service](../services/inventory-service.md)).
- The caller's team stays involved for graceful degradation.

## Prevention
- Set timeouts and retry budgets per dependency, and add circuit breakers.
- Alert on the dependency's latency SLO, not only on the caller's errors.
- Avoid remote calls inside database transactions.
