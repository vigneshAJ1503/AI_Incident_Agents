# sample-services

Four small microservices and a traffic generator, built from **one** image selected by `SERVICE_NAME`. They are the "production" system that AI Incident Agents investigates.

```
traffic-generator ──> order-service ──> inventory-service ──> Postgres
                           │
                           └──> payment-service ──> user-service ──> Postgres, Redis
                                     └──> Postgres (pool), Redis
```

| Endpoint | Service |
|----------|---------|
| `POST /api/v1/orders`, `GET /api/v1/orders/{id}` | order-service |
| `POST /api/v1/pay`, `GET /api/v1/payments/{id}` | payment-service |
| `POST /api/v1/login`, `GET /api/v1/users/{id}` | user-service |
| `GET /api/v1/stock/{sku}`, `POST /api/v1/reservations` | inventory-service |
| `/health`, `/ready`, `/metrics` | all |

- **Logs:** one JSON object per line, with the same fields as the synthetic generator (`aiops/seed/logs.py`): `@timestamp`, `level`, `message`, `service`, `environment`, `version`, `host`, `logger`, `trace_id`, `http_method`, `endpoint`, `status_code`, `duration_ms`, `error_type`.
- **Metrics:** the contract of `deploy/compose/config/prometheus/alert-rules.yml`: `http_requests_total`, `http_request_duration_seconds`, `db_pool_connections_{active,max,pending}`, `redis_up` (emitted by the apps that use Redis), all labelled `service`, `team`, `namespace`.

## Fault flags (per-service ConfigMap in `deploy/k8s/base`, used by PR-017)
| Variable / change | Normal | Incident |
|----------|---------|----------|
| `DB_POOL_SIZE` (payment) | 20 | **S1:** `2` → "Database connection timeout …" + HTTP 500 |
| `DB_HOLD_MS` (payment) | 800 | time a payment holds its DB connection (sized so a pool of 2 collapses under normal traffic) |
| `LEAK_KB_PER_REQUEST` (order) | 0 | **S2:** > 0 → memory grows until OOMKilled |
| `SLOW_QUERY_MS` (inventory) | 20 | **S3:** ≥ 3000 → slow queries, order-service timeouts |
| image tag (user) | `0.1.0` | **S4:** a nonexistent tag → ImagePullBackOff |
| Redis replicas | 1 | **S5:** `0` → "Redis connection refused" |

## Develop
```bash
make check-sample   # ruff, mypy, pytest
make k8s-up         # build into Minikube and deploy
```
