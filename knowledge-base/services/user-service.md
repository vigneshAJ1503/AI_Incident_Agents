---
title: user-service
type: service
services: [user-service]
tags: [service, identity, login, ownership]
owner: identity
last_reviewed: 2026-09-01
---
# user-service

## Overview
user-service manages user accounts, authentication (login and token issuance) and profiles. Login and checkout both depend on it, so reduced capacity here shows up as failures in several user journeys.

## Ownership
- **Team:** identity
- **On-call:** `#identity-oncall` (PagerDuty schedule "Identity Primary")
- **Escalation:** identity engineering manager, then `#incidents`
- **Tier:** 1

## Dependencies
| Dependency | Why | Failure impact |
|------------|-----|----------------|
| Postgres (`users` database) | Accounts | Pool exhaustion causes HTTP 500 on login |
| Redis | Sessions and token cache | Falls back to Postgres, so login latency rises |

## Endpoints
- `POST /api/v1/login`: authenticate
- `GET /api/v1/users/{id}`: profile (used by payment-service and order-service)

## Deployment
- 3 replicas in `prod`, `maxUnavailable: 1`. With fewer than 2 ready replicas the request queue grows (`Request queue depth high`) and callers get HTTP 503.
- Images: `registry.local/user-service:<version>`. The tag must exist before the manifest is updated.

## Dashboards
- Kibana logs: Discover, filter `service: user-service` (index `user-prod-*`)
- Grafana: "user-service overview" (login rate, 5xx rate, p95 latency, ready replicas)

## SLOs
- Login availability 99.9%
- p95 latency of `POST /api/v1/login` below 500 ms

## Known failure modes
- Bad image or stalled rollout: [bad-deployment-rollback](../runbooks/bad-deployment-rollback.md)
- Crash loops at startup: [pod-crashloop](../runbooks/pod-crashloop.md)
