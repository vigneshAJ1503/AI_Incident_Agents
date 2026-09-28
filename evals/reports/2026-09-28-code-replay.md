# Eval scorecard: `code` agent (replay)

Generated 2026-09-28 14:36 UTC · environment `local` · model `fake` · prompt `code/v2@6207b690c623+providers/code/git/v1@9750ac9fd68a`

Replay: recorded MCP fixtures (window ending 2026-09-25T10:30:00+00:00) and a scripted LLM that submits the agent's deterministic overview with status `no_signal`. Zero tokens. This scores the deterministic investigation and guardrails, not LLM reasoning.

Reproduce: `make eval AGENT=code MODE=replay`

## Summary

| Metric | Value |
|--------|-------|
| Pass rate | 6/6 (100%) |
| False-positive rate (healthy scenarios) | 0/1 (0%) |
| Findings with valid evidence citations | 6/6 (100%) |
| Avg tool calls | 4 |
| Avg LLM calls | 1 |
| Avg tokens (total) | 0 (0) |
| Latency avg / p50 | 2 ms / 2 ms |

## Scenarios

| Scenario | Result | Status | Signals | Evidence | Tool calls | LLM calls | Tokens | Latency |
|----------|--------|--------|---------|---------:|-----------:|----------:|-------:|--------:|
| S0 Healthy baseline (no incident) | PASS | no_signal | no_recent_changes | 3 | 3 | 1 | 0 | 3 ms |
| S1 DB connection pool misconfiguration (payment-service v1.8.2) | PASS | success | risky_config_change, recent_deployment_change, image_tag_change | 5 | 5 | 1 | 0 | 2 ms |
| S2 Memory leak -> OOM restarts (order-service) | PASS | success | risky_config_change, recent_deployment_change | 5 | 5 | 1 | 0 | 2 ms |
| S3 Slow downstream dependency (inventory-service -> order-service) | PASS | success | dependency_service_change | 5 | 5 | 1 | 0 | 2 ms |
| S4 Bad deployment (user-service image cannot be pulled) | PASS | success | recent_deployment_change, image_tag_change, unreleased_image_tag | 3 | 3 | 1 | 0 | 1 ms |
| S5 Cache outage (Redis down) | PASS | no_signal | no_recent_changes | 3 | 3 | 1 | 0 | 1 ms |

## Checks

### S0: PASS

- [x] `status`: got no_signal, expected one of ['no_signal']
- [x] `min_evidence`: 3 evidence items (min 2)
- [x] `signal:no_recent_changes`
- [x] `no_signal:risky_config_change`
- [x] `no_signal:recent_deployment_change`
- [x] `no_signal:image_tag_change`
- [x] `no_signal:unreleased_image_tag`
- [x] `no_signal:dependency_change`
- [x] `no_signal:schema_migration`
- [x] `no_signal:dependency_service_change`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S1: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 5 evidence items (min 3)
- [x] `signal:risky_config_change`
- [x] `signal:recent_deployment_change`
- [x] `no_signal:no_recent_changes`
- [x] `no_signal:unreleased_image_tag`
- [x] `no_signal:dependency_service_change`
- [x] `mentions:tune db pool`
- [x] `mentions:DB_POOL_SIZE`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S2: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 5 evidence items (min 3)
- [x] `signal:risky_config_change`
- [x] `signal:recent_deployment_change`
- [x] `no_signal:no_recent_changes`
- [x] `no_signal:image_tag_change`
- [x] `mentions:memory`
- [x] `mentions:cache`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S3: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 5 evidence items (min 3)
- [x] `signal:dependency_service_change`
- [x] `no_signal:no_recent_changes`
- [x] `no_signal:risky_config_change`
- [x] `no_signal:image_tag_change`
- [x] `mentions:inventory-service`
- [x] `mentions:idx_stock_levels_sku`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S4: PASS

- [x] `status`: got success, expected one of ['success']
- [x] `min_evidence`: 3 evidence items (min 3)
- [x] `signal:image_tag_change`
- [x] `signal:unreleased_image_tag`
- [x] `signal:recent_deployment_change`
- [x] `no_signal:no_recent_changes`
- [x] `no_signal:risky_config_change`
- [x] `mentions:v3.2.0`
- [x] `evidence_citations`: 1/1 findings cite existing evidence

### S5: PASS

- [x] `status`: got no_signal, expected one of ['no_signal']
- [x] `min_evidence`: 3 evidence items (min 2)
- [x] `signal:no_recent_changes`
- [x] `no_signal:risky_config_change`
- [x] `no_signal:recent_deployment_change`
- [x] `no_signal:image_tag_change`
- [x] `no_signal:unreleased_image_tag`
- [x] `no_signal:dependency_change`
- [x] `no_signal:schema_migration`
- [x] `no_signal:dependency_service_change`
- [x] `evidence_citations`: 1/1 findings cite existing evidence
