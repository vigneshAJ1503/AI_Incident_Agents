# Eval scorecard: `tickets` agent (replay)

Generated 2026-09-25 19:06 UTC · environment `local` · model `fake` · prompt `tickets/v1@2d64a95b8638`

Replay: recorded MCP fixtures (window ending 2026-09-25T10:30:00+00:00) and a scripted LLM that submits the agent's deterministic overview with status `no_signal`. Zero tokens. This scores the deterministic investigation and guardrails, not LLM reasoning.

Reproduce: `make eval AGENT=tickets MODE=replay`

## Summary

| Metric | Value |
|--------|-------|
| Pass rate | 6/6 (100%) |
| False-positive rate (healthy scenarios) | 0/1 (0%) |
| Findings with valid evidence citations | 6/6 (100%) |
| Avg tool calls | 1.5 |
| Avg LLM calls | 1 |
| Avg tokens (total) | 0 (0) |
| Latency avg / p50 | 2 ms / 2 ms |

## Scenarios

| Scenario | Result | Status | Signals | Evidence | Tool calls | LLM calls | Tokens | Latency |
|----------|--------|--------|---------|---------:|-----------:|----------:|-------:|--------:|
| S0 Healthy baseline (no incident) | PASS | no_signal | related_open_tickets | 6 | 1 | 1 | 0 | 4 ms |
| S1 DB connection pool misconfiguration (payment-service v1.8.2) | PASS | success | known_issue_open, related_open_tickets | 7 | 2 | 1 | 0 | 2 ms |
| S2 Memory leak -> OOM restarts (order-service) | PASS | no_signal | related_open_tickets | 9 | 1 | 1 | 0 | 2 ms |
| S3 Slow downstream dependency (inventory-service -> order-service) | PASS | success | dependency_known_issue_open, related_open_tickets | 10 | 2 | 1 | 0 | 2 ms |
| S4 Bad deployment (user-service image cannot be pulled) | PASS | no_signal | related_open_tickets | 3 | 1 | 1 | 0 | 1 ms |
| S5 Cache outage (Redis down) | PASS | success | similar_past_incident, related_open_tickets | 8 | 2 | 1 | 0 | 2 ms |

## Checks

### S0: PASS

- [x] `status`: got no_signal, expected one of ['no_signal']
- [x] `min_evidence`: 6 evidence items (min 1)
- [x] `no_signal:known_issue_open`
- [x] `no_signal:dependency_known_issue_open`
- [x] `no_signal:similar_past_incident`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S1: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 7 evidence items (min 2)
- [x] `signal:known_issue_open`
- [x] `mentions:OPS-12`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S2: PASS

- [x] `status`: got no_signal, expected one of ['no_signal']
- [x] `min_evidence`: 9 evidence items (min 1)
- [x] `no_signal:known_issue_open`
- [x] `no_signal:dependency_known_issue_open`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S3: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 10 evidence items (min 2)
- [x] `signal:dependency_known_issue_open`
- [x] `no_signal:known_issue_open`
- [x] `mentions:inventory`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S4: PASS

- [x] `status`: got no_signal, expected one of ['no_signal']
- [x] `min_evidence`: 3 evidence items (min 1)
- [x] `no_signal:known_issue_open`
- [x] `no_signal:dependency_known_issue_open`
- [x] `no_signal:similar_past_incident`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S5: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 8 evidence items (min 2)
- [x] `signal:similar_past_incident`
- [x] `no_signal:known_issue_open`
- [x] `mentions:redis`
- [x] `evidence_citations`: 1/1 findings cite existing evidence
