# Eval scorecard: `logs` agent (replay)

Generated 2026-09-25 18:45 UTC · environment `local` · model `fake` · prompt `logs/v2@cd449df3a662`

Replay: recorded MCP fixtures (window ending 2026-09-25T10:30:00+00:00) and a scripted LLM that submits the agent's deterministic overview with status `no_signal`. Zero tokens. This scores the deterministic investigation and guardrails, not LLM reasoning.

Reproduce: `make eval AGENT=logs MODE=replay`

## Summary

| Metric | Value |
|--------|-------|
| Pass rate | 6/6 (100%) |
| False-positive rate (healthy scenarios) | 0/1 (0%) |
| Findings with valid evidence citations | 6/6 (100%) |
| Avg tool calls | 3.83 |
| Avg LLM calls | 1 |
| Avg tokens (total) | 0 (0) |
| Latency avg / p50 | 4 ms / 4 ms |

## Scenarios

| Scenario | Result | Status | Signals | Evidence | Tool calls | LLM calls | Tokens | Latency |
|----------|--------|--------|---------|---------:|-----------:|----------:|-------:|--------:|
| S0 Healthy baseline (no incident) | PASS | no_signal | - | 3 | 3 | 1 | 0 | 4 ms |
| S1 DB connection pool misconfiguration (payment-service v1.8.2) | PASS | success | db_timeout_errors_up, error_rate_up, new_error_pattern, deployment_detected | 4 | 4 | 1 | 0 | 4 ms |
| S2 Memory leak -> OOM restarts (order-service) | PASS | success | error_rate_up, new_error_pattern, oom_errors, service_restarts | 4 | 4 | 1 | 0 | 3 ms |
| S3 Slow downstream dependency (inventory-service -> order-service) | PASS | success | error_rate_up, new_error_pattern, dependency_timeouts | 4 | 4 | 1 | 0 | 3 ms |
| S4 Bad deployment (user-service image cannot be pulled) | PASS | success | capacity_degraded | 4 | 4 | 1 | 0 | 5 ms |
| S5 Cache outage (Redis down) | PASS | success | error_rate_up, new_error_pattern, cache_connection_errors | 4 | 4 | 1 | 0 | 6 ms |

## Checks

### S0: PASS

- [x] `status`: got no_signal, expected one of ['no_signal']
- [x] `min_evidence`: 3 evidence items (min 1)
- [x] `no_signal:db_timeout_errors_up`
- [x] `no_signal:error_rate_up`
- [x] `no_signal:new_error_pattern`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S1: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 4 evidence items (min 1)
- [x] `signal:db_timeout_errors_up`
- [x] `mentions:connection`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S2: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 4 evidence items (min 1)
- [x] `signal:oom_errors`
- [x] `mentions:memory`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S3: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 4 evidence items (min 1)
- [x] `signal:dependency_timeouts`
- [x] `mentions:inventory`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S4: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 4 evidence items (min 1)
- [x] `signal:capacity_degraded`
- [x] `mentions:replicas`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S5: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 4 evidence items (min 1)
- [x] `signal:cache_connection_errors`
- [x] `mentions:redis`
- [x] `evidence_citations`: 1/1 findings cite existing evidence
