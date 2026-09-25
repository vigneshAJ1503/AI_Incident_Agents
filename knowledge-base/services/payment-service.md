---
title: payment-service
type: service
services: [payment-service]
tags: [service, payments, ownership]
owner: payments
last_reviewed: 2026-09-01
---
# payment-service

## Overview
payment-service processes card payments for orders. It authorizes and captures payments through the card processor, and stores payment records and idempotency keys. It is on the checkout critical path: if it's down, no order can be paid.

## Ownership
- **Team:** payments
- **On-call:** `#payments-oncall` (PagerDuty schedule "Payments Primary")
- **Escalation:** payments engineering manager, then the SRE incident commander in `#incidents`
- **Tier:** 1 (customer-facing, revenue-critical)

## Dependencies
| Dependency | Why | Failure impact |
|------------|-----|----------------|
| Postgres (`payments` database) | Payment records | Connection pool exhaustion causes HTTP 500 on `POST /api/v1/pay` |
| Redis | Idempotency keys, session cache | Falls back to Postgres, so latency rises |
| user-service | Account and fraud checks | Payments fail with 502/503 when user-service is degraded |

## Endpoints
- `POST /api/v1/pay`: authorize and capture a payment (p95 SLO 800 ms)
- `GET /api/v1/payments/{id}`: payment status
- `POST /api/v1/refunds`: refunds

## Configuration
| Setting | Default | Notes |
|---------|---------|-------|
| `DB_POOL_SIZE` | 20 | Keep `replicas × DB_POOL_SIZE` below Postgres `max_connections`. **Never below 10 in production.** |
| `DB_POOL_TIMEOUT_MS` | 5000 | Wait for a free connection before failing with HTTP 500 |
| `REDIS_URL` | `redis://redis:6379` | |
| `USER_SERVICE_TIMEOUT_MS` | 1000 | |

## Dashboards
- Kibana logs: Discover, filter `service: payment-service` (index `payment-prod-*`)
- Grafana: "payment-service overview" (request rate, 5xx rate, p95 latency, DB pool active/max)
- Alertmanager: `service="payment-service"`

## SLOs
- Availability 99.9% (5xx rate below 0.1% over 30 days)
- p95 latency of `POST /api/v1/pay` below 800 ms

## Known failure modes
- DB pool exhaustion after config changes: [database-connection-pool](../runbooks/database-connection-pool.md)
- Cache outage: [redis-outage](../runbooks/redis-outage.md)
- Bad releases: [bad-deployment-rollback](../runbooks/bad-deployment-rollback.md)
