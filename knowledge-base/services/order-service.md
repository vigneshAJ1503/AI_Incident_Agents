---
title: order-service
type: service
services: [order-service]
tags: [service, orders, checkout, ownership]
owner: commerce
last_reviewed: 2026-09-01
---
# order-service

## Overview
order-service creates and tracks customer orders. At checkout it reserves stock in inventory-service, takes the payment through payment-service, and stores the order. It is the entry point for checkout traffic.

## Ownership
- **Team:** commerce
- **On-call:** `#commerce-oncall` (PagerDuty schedule "Commerce Primary")
- **Escalation:** commerce engineering manager, then `#incidents`
- **Tier:** 1

## Dependencies
| Dependency | Why | Failure impact |
|------------|-----|----------------|
| inventory-service | Stock checks and reservations (`GET /api/v1/stock/{sku}`) | A slow inventory-service causes `Timeout calling inventory-service` and HTTP 504 |
| payment-service | Payment for the order | Payment errors fail the checkout |
| Postgres (`orders` database) | Order records | Pool exhaustion causes HTTP 500 |

## Endpoints
- `POST /api/v1/orders`: create an order (checkout)
- `GET /api/v1/orders/{id}`: order status

## Configuration
| Setting | Default | Notes |
|---------|---------|-------|
| `INVENTORY_TIMEOUT_MS` | 3000 | Client timeout for inventory-service calls |
| `INVENTORY_RETRIES` | 1 | Keep this low; retries amplify a slow dependency |
| `JAVA_OPTS` | `-XX:MaxRAMPercentage=75` | |
| Memory limit | 512Mi | OOMKilled when exceeded |

## Dashboards
- Kibana logs: Discover, filter `service: order-service` (index `order-prod-*`)
- Grafana: "order-service overview" (checkout rate, 5xx/504 rate, p95 latency, JVM heap, restarts)

## SLOs
- Checkout success 99.5%
- p95 latency of `POST /api/v1/orders` below 1.5 s

## Known failure modes
- Memory leak and OOM restarts: [memory-leak-oom](../runbooks/memory-leak-oom.md)
- Slow inventory-service: [dependency-timeouts](../runbooks/dependency-timeouts.md)
