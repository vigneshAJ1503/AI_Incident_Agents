# Eval scorecard: `knowledge` agent (replay)

Generated 2026-09-25 19:06 UTC · environment `local` · model `fake` · prompt `knowledge/v1@92f99521e906`

Replay: recorded MCP fixtures (window ending 2026-09-25T10:30:00+00:00) and a scripted LLM that submits the agent's deterministic overview with status `no_signal`. Zero tokens. This scores the deterministic investigation and guardrails, not LLM reasoning.

Reproduce: `make eval AGENT=knowledge MODE=replay`

## Summary

| Metric | Value |
|--------|-------|
| Pass rate | 6/6 (100%) |
| False-positive rate (healthy scenarios) | 0/1 (0%) |
| Findings with valid evidence citations | 43/43 (100%) |
| Avg tool calls | 6.83 |
| Avg LLM calls | 1 |
| Avg tokens (total) | 0 (0) |
| Latency avg / p50 | 6 ms / 7 ms |

## Scenarios

| Scenario | Result | Status | Signals | Evidence | Tool calls | LLM calls | Tokens | Latency |
|----------|--------|--------|---------|---------:|-----------:|----------:|-------:|--------:|
| S0 Healthy baseline (no incident) | PASS | no_signal | no_relevant_docs | 2 | 2 | 1 | 0 | 2 ms |
| S1 DB connection pool misconfiguration (payment-service v1.8.2) | PASS | success | runbook_found, known_issue_documented, mitigation_available | 9 | 9 | 1 | 0 | 7 ms |
| S2 Memory leak -> OOM restarts (order-service) | PASS | success | runbook_found, known_issue_documented, mitigation_available | 6 | 6 | 1 | 0 | 5 ms |
| S3 Slow downstream dependency (inventory-service -> order-service) | PASS | success | runbook_found, known_issue_documented, mitigation_available | 9 | 9 | 1 | 0 | 10 ms |
| S4 Bad deployment (user-service image cannot be pulled) | PASS | success | runbook_found, known_issue_documented, mitigation_available | 7 | 7 | 1 | 0 | 7 ms |
| S5 Cache outage (Redis down) | PASS | success | runbook_found, known_issue_documented, mitigation_available | 8 | 8 | 1 | 0 | 7 ms |

## Checks

### S0: PASS

- [x] `status`: got no_signal, expected one of ['no_signal']
- [x] `min_evidence`: 2 evidence items (min 1)
- [x] `signal:no_relevant_docs`
- [x] `no_signal:runbook_found`
- [x] `no_signal:known_issue_documented`
- [x] `no_signal:mitigation_available`
- [x] `evidence_citations`: 2/2 findings cite existing evidence

### S1: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 9 evidence items (min 3)
- [x] `signal:runbook_found`
- [x] `signal:known_issue_documented`
- [x] `signal:mitigation_available`
- [x] `no_signal:no_relevant_docs`
- [x] `mentions:knowledge-base/runbooks/database-connection-pool.md`
- [x] `evidence_citations`: 9/9 findings cite existing evidence

### S2: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 6 evidence items (min 3)
- [x] `signal:runbook_found`
- [x] `signal:known_issue_documented`
- [x] `signal:mitigation_available`
- [x] `no_signal:no_relevant_docs`
- [x] `mentions:knowledge-base/runbooks/memory-leak-oom.md`
- [x] `evidence_citations`: 7/7 findings cite existing evidence

### S3: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 9 evidence items (min 3)
- [x] `signal:runbook_found`
- [x] `signal:known_issue_documented`
- [x] `signal:mitigation_available`
- [x] `no_signal:no_relevant_docs`
- [x] `mentions:knowledge-base/runbooks/dependency-timeouts.md`
- [x] `evidence_citations`: 9/9 findings cite existing evidence

### S4: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 7 evidence items (min 3)
- [x] `signal:runbook_found`
- [x] `signal:known_issue_documented`
- [x] `signal:mitigation_available`
- [x] `no_signal:no_relevant_docs`
- [x] `mentions:knowledge-base/runbooks/bad-deployment-rollback.md`
- [x] `evidence_citations`: 8/8 findings cite existing evidence

### S5: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 8 evidence items (min 3)
- [x] `signal:runbook_found`
- [x] `signal:known_issue_documented`
- [x] `signal:mitigation_available`
- [x] `no_signal:no_relevant_docs`
- [x] `mentions:knowledge-base/runbooks/redis-outage.md`
- [x] `evidence_citations`: 8/8 findings cite existing evidence
