# Eval scorecard: `metrics` agent (replay)

Generated 2026-09-28 15:06 UTC · environment `local` · model `fake` · prompt `metrics/v2@a574a2e1416d+providers/metrics/prometheus/v1@030b71373da6`

Replay: recorded MCP fixtures (window ending 2026-09-25T10:30:00+00:00) and a scripted LLM that submits the agent's deterministic overview with status `no_signal`. Zero tokens. This scores the deterministic investigation and guardrails, not LLM reasoning.

Reproduce: `make eval AGENT=metrics MODE=replay`

## Summary

| Metric | Value |
|--------|-------|
| Pass rate | 6/6 (100%) |
| False-positive rate (healthy scenarios) | 0/1 (0%) |
| Findings with valid evidence citations | 6/6 (100%) |
| Avg tool calls | 10 |
| Avg LLM calls | 1 |
| Avg tokens (total) | 0 (0) |
| Latency avg / p50 | 5 ms / 6 ms |

## Scenarios

| Scenario | Result | Status | Signals | Evidence | Tool calls | LLM calls | Tokens | Latency |
|----------|--------|--------|---------|---------:|-----------:|----------:|-------:|--------:|
| S0 Healthy baseline (no incident) | PASS | no_signal | no_anomaly | 10 | 10 | 1 | 0 | 6 ms |
| S1 DB connection pool misconfiguration (payment-service v1.8.2) | PASS | success | error_rate_up, latency_up, db_pool_saturated | 10 | 10 | 1 | 0 | 5 ms |
| S2 Memory leak -> OOM restarts (order-service) | PASS | success | traffic_drop, memory_pressure | 10 | 10 | 1 | 0 | 6 ms |
| S3 Slow downstream dependency (inventory-service -> order-service) | PASS | success | error_rate_up, latency_up, dependency_latency_up | 10 | 10 | 1 | 0 | 6 ms |
| S4 Bad deployment (user-service image cannot be pulled) | PASS | success | traffic_drop | 10 | 10 | 1 | 0 | 4 ms |
| S5 Cache outage (Redis down) | PASS | success | latency_up, cache_down, dependency_latency_up | 10 | 10 | 1 | 0 | 5 ms |

## Checks

### S0: PASS

- [x] `status`: got no_signal, expected one of ['no_signal']
- [x] `min_evidence`: 10 evidence items (min 10)
- [x] `signal:no_anomaly`
- [x] `no_signal:error_rate_up`
- [x] `no_signal:latency_up`
- [x] `no_signal:traffic_drop`
- [x] `no_signal:traffic_spike`
- [x] `no_signal:db_pool_saturated`
- [x] `no_signal:memory_pressure`
- [x] `no_signal:cache_down`
- [x] `no_signal:dependency_latency_up`
- [x] `mentions:No metric anomaly`
- [x] `mentions:payment-service`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S1: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 10 evidence items (min 10)
- [x] `signal:error_rate_up`
- [x] `signal:db_pool_saturated`
- [x] `signal:latency_up`
- [x] `no_signal:no_anomaly`
- [x] `no_signal:cache_down`
- [x] `no_signal:memory_pressure`
- [x] `no_signal:dependency_latency_up`
- [x] `mentions:payment-service`
- [x] `mentions:error rate up`
- [x] `mentions:db pool`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S2: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 10 evidence items (min 10)
- [x] `signal:memory_pressure`
- [x] `signal:traffic_drop`
- [x] `no_signal:no_anomaly`
- [x] `no_signal:cache_down`
- [x] `no_signal:db_pool_saturated`
- [x] `no_signal:dependency_latency_up`
- [x] `mentions:order-service`
- [x] `mentions:memory rss up`
- [x] `mentions:OOMKilled`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S3: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 10 evidence items (min 10)
- [x] `signal:latency_up`
- [x] `signal:dependency_latency_up`
- [x] `signal:error_rate_up`
- [x] `no_signal:no_anomaly`
- [x] `no_signal:cache_down`
- [x] `no_signal:db_pool_saturated`
- [x] `no_signal:memory_pressure`
- [x] `mentions:inventory-service`
- [x] `mentions:latency p95 up`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S4: PASS

- [x] `status`: got success, expected one of ['success', 'no_signal']
- [x] `min_evidence`: 10 evidence items (min 10)
- [x] `no_signal:error_rate_up`
- [x] `no_signal:latency_up`
- [x] `no_signal:db_pool_saturated`
- [x] `no_signal:memory_pressure`
- [x] `no_signal:cache_down`
- [x] `no_signal:dependency_latency_up`
- [x] `mentions:user-service`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S5: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 10 evidence items (min 10)
- [x] `signal:cache_down`
- [x] `signal:latency_up`
- [x] `signal:dependency_latency_up`
- [x] `no_signal:no_anomaly`
- [x] `no_signal:error_rate_up`
- [x] `no_signal:db_pool_saturated`
- [x] `no_signal:memory_pressure`
- [x] `mentions:payment-service`
- [x] `mentions:cache DOWN`
- [x] `mentions:latency p95 up`
- [x] `evidence_citations`: 1/1 findings cite existing evidence
