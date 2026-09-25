# Eval scorecard: `alerts` agent (replay)

Generated 2026-09-25 19:20 UTC · environment `local` · model `fake` · prompt `alerts/v1@ecf130b53c6e`

Replay: recorded MCP fixtures (window ending 2026-09-25T10:30:00+00:00) and a scripted LLM that submits the agent's deterministic overview with status `no_signal`. Zero tokens. This scores the deterministic investigation and guardrails, not LLM reasoning.

Reproduce: `make eval AGENT=alerts MODE=replay`

## Summary

| Metric | Value |
|--------|-------|
| Pass rate | 6/6 (100%) |
| False-positive rate (healthy scenarios) | 0/1 (0%) |
| Findings with valid evidence citations | 6/6 (100%) |
| Avg tool calls | 4.83 |
| Avg LLM calls | 1 |
| Avg tokens (total) | 0 (0) |
| Latency avg / p50 | 2 ms / 2 ms |

## Scenarios

| Scenario | Result | Status | Signals | Evidence | Tool calls | LLM calls | Tokens | Latency |
|----------|--------|--------|---------|---------:|-----------:|----------:|-------:|--------:|
| S0 Healthy baseline (no incident) | PASS | no_signal | no_active_alerts | 5 | 5 | 1 | 0 | 2 ms |
| S1 DB connection pool misconfiguration (payment-service v1.8.2) | PASS | success | alerts_firing, critical_alert_firing | 5 | 5 | 1 | 0 | 2 ms |
| S2 Memory leak -> OOM restarts (order-service) | PASS | success | alerts_firing, critical_alert_firing | 5 | 5 | 1 | 0 | 2 ms |
| S3 Slow downstream dependency (inventory-service -> order-service) | PASS | success | alerts_firing, critical_alert_firing, dependency_alert_firing | 5 | 5 | 1 | 0 | 1 ms |
| S4 Bad deployment (user-service image cannot be pulled) | PASS | success | alerts_firing | 4 | 4 | 1 | 0 | 1 ms |
| S5 Cache outage (Redis down) | PASS | success | alerts_firing, critical_alert_firing, dependency_alert_firing | 5 | 5 | 1 | 0 | 2 ms |

## Checks

### S0: PASS

- [x] `status`: got no_signal, expected one of ['no_signal']
- [x] `min_evidence`: 5 evidence items (min 1)
- [x] `signal:no_active_alerts`
- [x] `no_signal:alerts_firing`
- [x] `no_signal:critical_alert_firing`
- [x] `no_signal:dependency_alert_firing`
- [x] `no_signal:alert_precedes_incident`
- [x] `mentions:no active alerts`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S1: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 5 evidence items (min 2)
- [x] `signal:alerts_firing`
- [x] `signal:critical_alert_firing`
- [x] `no_signal:no_active_alerts`
- [x] `no_signal:dependency_alert_firing`
- [x] `no_signal:alert_precedes_incident`
- [x] `mentions:HighErrorRate`
- [x] `mentions:DatabaseConnectionPoolExhausted`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S2: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 5 evidence items (min 2)
- [x] `signal:alerts_firing`
- [x] `signal:critical_alert_firing`
- [x] `no_signal:no_active_alerts`
- [x] `no_signal:dependency_alert_firing`
- [x] `no_signal:alert_precedes_incident`
- [x] `mentions:PodOOMKilled`
- [x] `mentions:PodCrashLooping`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S3: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 5 evidence items (min 2)
- [x] `signal:alerts_firing`
- [x] `signal:critical_alert_firing`
- [x] `signal:dependency_alert_firing`
- [x] `no_signal:no_active_alerts`
- [x] `no_signal:alert_precedes_incident`
- [x] `mentions:inventory-service`
- [x] `mentions:HighLatencyP95`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S4: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 4 evidence items (min 2)
- [x] `signal:alerts_firing`
- [x] `no_signal:no_active_alerts`
- [x] `no_signal:critical_alert_firing`
- [x] `no_signal:dependency_alert_firing`
- [x] `no_signal:alert_precedes_incident`
- [x] `mentions:DeploymentReplicasMismatch`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S5: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 5 evidence items (min 2)
- [x] `signal:alerts_firing`
- [x] `signal:critical_alert_firing`
- [x] `signal:dependency_alert_firing`
- [x] `no_signal:no_active_alerts`
- [x] `no_signal:alert_precedes_incident`
- [x] `mentions:RedisDown`
- [x] `evidence_citations`: 1/1 findings cite existing evidence
