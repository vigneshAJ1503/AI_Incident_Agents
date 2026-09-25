---
title: inventory-service
type: service
services: [inventory-service]
tags: [service, inventory, stock, ownership]
owner: commerce
last_reviewed: 2026-09-01
---
# inventory-service

## Overview
inventory-service owns stock levels and reservations. order-service calls it on every checkout to check and reserve stock, so its latency adds directly to checkout latency.

## Ownership
- **Team:** commerce
- **On-call:** `#commerce-oncall`
- **Escalation:** commerce engineering manager, `#dba-oncall` for database issues
- **Tier:** 2 (checkout degrades, but cached stock levels can be served)

## Dependencies
| Dependency | Why | Failure impact |
|------------|-----|----------------|
| Postgres (`inventory` database, table `stock_levels`) | Stock data | Slow queries make callers time out |

## Endpoints
- `GET /api/v1/stock/{sku}`: stock level (p95 SLO 200 ms)
- `POST /api/v1/reservations`: reserve stock for an order

## Dashboards
- Kibana logs: Discover, filter `service: inventory-service` (index `inventory-prod-*`)
- Grafana: "inventory-service overview" (request rate, p95 latency, slow queries)

## Known failure modes
- Slow `stock_levels` queries (missing index): [slow-database-queries](../runbooks/slow-database-queries.md). Callers see [dependency-timeouts](../runbooks/dependency-timeouts.md).
