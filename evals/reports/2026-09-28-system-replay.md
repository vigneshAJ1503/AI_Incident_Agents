# System scorecard (replay)

Replay: recorded MCP fixtures and a scripted LLM (zero tokens, deterministic). This scores the deterministic pipeline (planning, agents, rule-based RCA, guardrails), not LLM reasoning; times are replay times, not live latency.

## Run

| | |
|---|---|
| Generated | 2026-09-28 19:52 UTC |
| Profile | `local` |
| LLM provider (configured) | `api.groq.com` |
| LLM used | scripted replay LLM (zero tokens) |
| Models (configured) | none |
| Models (used) | fake |
| Git SHA | `5c1db6f6fc08` |
| LLM judge | off (rule-based root-cause scoring only; enable with --judge) |
| Reproduce | `aiops evaluate --mode replay --profile local` |

## Regression gate

- PASS: no metric is worse than the baseline.

## Investigations (end to end)

| Metric | Value | v1.0 target |
|--------|-------|-------------|
| Root-cause accuracy, rule-based (incident scenarios) | 5/5 (100%) | >= 4/5 |
| Root-cause accuracy, LLM judge | not judged | |
| False positives (healthy scenarios) | 0/1 (0%) | 0 |
| All checks pass | 6/6 (100%) | |
| Confidence calibration (Brier score, 0 = best) | 0.005 | |
| Claims with valid evidence citations | 81/81 (100%) | >= 95% |
| Time to report avg / p50 | 90 ms / 89 ms | p50 < 90 s |
| Tokens total (avg) | 0 (0) | |
| Cost estimate total (avg) | $0.0000 ($0.0000) | tracked |

| Scenario | Result | Service | Root cause (rule) | Judge | Confidence | Severity | Time | Tokens |
|----------|--------|---------|-------------------|-------|-----------:|----------|-----:|-------:|
| S0 Healthy baseline (no incident) | PASS | payment-service | none (healthy) | - | 0.00 | none | 65 ms | 0 |
| S1 DB connection pool misconfiguration (payment-service v1.8.2) | PASS | payment-service | correct | - | 0.95 | critical | 90 ms | 0 |
| S2 Memory leak -> OOM restarts (order-service) | PASS | order-service | correct | - | 0.95 | high | 119 ms | 0 |
| S3 Slow downstream dependency (inventory-service -> order-service) | PASS | order-service | correct | - | 0.94 | high | 94 ms | 0 |
| S4 Bad deployment (user-service image cannot be pulled) | PASS | user-service | correct | - | 0.91 | high | 86 ms | 0 |
| S5 Cache outage (Redis down) | PASS | payment-service | correct | - | 0.88 | high | 89 ms | 0 |

- **S1** root cause: Database connection pool misconfiguration introduced by a recent change to payment-service (v1.8.2, DB_POOL_SIZE 20 -> 2) causes connection timeouts and HTTP 500s.
- **S2** root cause: Memory leak in order-service causes OutOfMemoryError and repeated container restarts (OOMKilled) (v2.3.0).
- **S3** root cause: Slow downstream dependency inventory-service causes order-service upstream timeouts.
- **S4** root cause: user-service deployment (v3.2.0) references an image tag that cannot be pulled (ImagePullBackOff), leaving it with fewer ready replicas.
- **S5** root cause: Cache outage (redis) forces fallback to the database, increasing latency for payment-service and every service that uses the cache.

## Planner (service identification from the question alone)

Service accuracy 88% · clarification when expected 100% · invented services **0** · all checks 11/12 (92%)

| Case | Question | Expected | Got | Result |
|------|----------|----------|-----|--------|
| S0 | Is anything wrong with payment-service in production? | payment-service | payment-service | PASS |
| S1 | Payment API is returning HTTP 500 in production | payment-service | payment-service | PASS |
| S2 | Orders are failing intermittently in production | order-service | order-service | PASS |
| S3 | Why are orders timing out in production? | order-service | order-service | PASS |
| S4 | Login and checkout requests are failing in production | user-service | clarification (user-service, order-service) | FAIL: service:user-service |
| S5 | Payments are slow in production | payment-service | payment-service | PASS |
| N1 | Something is broken | clarification | clarification (payment-service, order-service, user-service, inventory-service) | PASS |
| N2 | checkout-gateway is returning 500s in production | clarification | clarification (payment-service, order-service, user-service, inventory-service) | PASS |
| N3 | The billing-service is down in prod | clarification | clarification (payment-service, order-service, user-service, inventory-service) | PASS |
| N4 | order-service and user-service are both failing | clarification | clarification (order-service, user-service) | PASS |
| P1 | checkout is down in prod | order-service | order-service | PASS |
| P2 | Payments are slow | payment-service | payment-service | PASS |

## Agents (each on its own, per scenario)

| Agent | Version | Pass rate | False positives | Valid citations | Avg tool calls | Avg tokens | p50 latency | Cost | Failed |
|-------|---------|-----------|-----------------|-----------------|---------------:|-----------:|------------:|-----:|--------|
| alerts | 1 | 6/6 (100%) | 0% | 100% | 4.83 | 0 | 2 ms | $0.0000 | - |
| code | 1 | 6/6 (100%) | 0% | 100% | 4 | 0 | 3 ms | $0.0000 | - |
| k8s | 1 | 6/6 (100%) | 0% | 100% | 4 | 0 | 3 ms | $0.0000 | - |
| knowledge | 1 | 6/6 (100%) | 0% | 100% | 6.83 | 0 | 7 ms | $0.0000 | - |
| logs | 2 | 6/6 (100%) | 0% | 100% | 3.83 | 0 | 4 ms | $0.0000 | - |
| metrics | 1 | 6/6 (100%) | 0% | 100% | 10 | 0 | 7 ms | $0.0000 | - |
| tickets | 1 | 6/6 (100%) | 0% | 100% | 1.5 | 0 | 3 ms | $0.0000 | - |

## Versions

| Component | Version |
|---|---|
| agent `alerts` | 1 |
| agent `code` | 1 |
| agent `k8s` | 1 |
| agent `knowledge` | 1 |
| agent `logs` | 2 |
| agent `metrics` | 1 |
| agent `orchestrator` | 1 |
| agent `rca` | 1 |
| agent `tickets` | 1 |
| prompt `alerts` | `alerts/v2@16a789f3f195+providers/alerts/alertmanager/v1@2a08404182c5` |
| prompt `code` | `code/v2@6207b690c623+providers/code/git/v1@9750ac9fd68a` |
| prompt `k8s` | `k8s/v1@19dad1dee16e` |
| prompt `knowledge` | `knowledge/v1@92f99521e906` |
| prompt `logs` | `logs/v3@80c6de519237+providers/logs/elasticsearch/v1@a5776d7915e3` |
| prompt `metrics` | `metrics/v2@a574a2e1416d+providers/metrics/prometheus/v1@030b71373da6` |
| prompt `tickets` | `tickets/v2@72cbcae151bb+providers/tickets/jira/v1@e280a5273c58` |
