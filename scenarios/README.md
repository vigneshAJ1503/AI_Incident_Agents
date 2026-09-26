# Incident scenarios

Each scenario is a reproducible incident with **ground truth**, used for agent tests and evals (MASTER_PLAN.md §6, §15).

```
scenarios/<id>/
├── README.md            # what happens, what the investigation should conclude
├── expected.yaml        # question, service, window, ground-truth root cause
└── agents/<agent>.yaml  # expectations for one agent (status, signals, ...)
```

- **Synthetic data:** PR-007 seeds log data for each scenario (`make seed-logs S=S1`); PR-023 seeds the matching Alertmanager alerts (`make seed-alerts S=S1`, see `docs/setup/alerts.md`).
- **Sample Git repo:** PR-026 builds `.data/sample-repo` for each scenario (`make seed-repo S=S1`): ~2 weeks of shared history, plus the scenario's change, timed consistently with the logs (incident at now−20m, S1 rollout at now−22m). Release tags are `<service>/<version>`.

  | Scenario | Change in the repo |
  |----------|--------------------|
  | S0 | none (only harmless docs/refactor/test commits) |
  | S1 | `tune db pool`: payment-service `DB_POOL_SIZE` 20 → 2 (now−2h10m), released as `payment-service/v1.8.2` (now−35m) |
  | S2 | order-service: unbounded in-memory order cache (now−5h) and memory limit 1Gi → 512Mi (now−1h10m) |
  | S3 | inventory-service: case-insensitive stock query + `DROP INDEX idx_stock_levels_sku` migration (now−3h) |
  | S4 | user-service manifest bumped to image `v3.2.0` (now−28m); no such release tag or image exists |
  | S5 | none: the Redis outage is infrastructure-level (a negative case for the Code agent) |
- **Live data:** PR-017 adds live fault injection (`inject.sh` / `revert.sh`).

`expected.yaml` fields:

| Field | Meaning |
|-------|---------|
| `question` | What the engineer asks |
| `service`, `environment`, `window` | The context the planner should extract |
| `root_cause` | Ground-truth root cause (`null` = healthy; agents must not invent one) |
| `agents/<name>.yaml` → `status` | Acceptable agent statuses |
| `signals` | Signals that must be reported |
| `forbidden_signals` | Signals that must NOT be reported |
| `must_mention` | Phrases the summary or findings must contain (case-insensitive) |
| `min_evidence` | Minimum number of evidence items |
| `hints` | Optional task input for the agent when it runs standalone (e.g. representative upstream findings such as the Log agent's signals and patterns); the orchestrator passes real ones |

## Evaluations

`make eval AGENT=<agent> MODE=replay|live` (or `aiops eval run --agent logs [--scenario S1 ...]`) runs an agent on every scenario that has `agents/<agent>.yaml` and writes a scorecard to `evals/reports/<date>-<agent>-<mode>.md` plus a JSON twin. It exits 1 when the pass rate is below `--min-pass-rate` (default 1.0 for replay, 0.8 for live).

| Mode | Data | LLM | Use |
|------|------|-----|-----|
| `replay` (default) | fixtures recorded at 2026-09-25T10:30Z in `backend/tests/fixtures/<agent>/<scenario>/` | scripted: submits the agent's deterministic overview with status `no_signal` (zero tokens) | CI (via pytest), every PR |
| `live` | each scenario seeded into the local Elasticsearch at the current time | the configured hosted LLM (fails fast if no key) | before tagging a release |

Fixtures recorded **live** against the real cluster (K8s agent, PR-019: `aiops fault run S<n> -- python -m tests.fixtures.record_k8s`) can't be anchored at the fixed time. Such a fixture directory has a `meta.json` (`start`, `end`, `incident_start`), and replay uses that window, with `incident_start` passed as a hint (`aiops.evals.replay.replay_task`).

Per scenario the scorecard records the `agents/<agent>.yaml` checks, evidence-citation validity (every cited evidence id exists), tool calls, LLM calls, tokens and latency. It also aggregates the pass rate, the **false-positive rate** (scenarios with `root_cause: null` where the agent reports `success` or any anomaly signal), and averages. A live run leaves the last scenario seeded; restore the default with `make seed-logs S=S1`.

### Plan datasets → scenarios

MASTER_PLAN PR-011 lists seven evaluation datasets. The seeded scenarios cover them as follows:

| Dataset | Scenario | Notes |
|---------|----------|-------|
| db-timeout | S1 | connection-pool timeouts after the v1.8.2 deployment |
| memory-leak | S2 | OutOfMemoryError + restarts |
| http-500 | S1 | HTTP 500 on `POST /api/v1/pay` (S4 adds 503s from callers) |
| network-timeout | S3 | upstream 504s: order-service → inventory-service |
| auth-failure | S0 (as noise) | `Invalid credentials` warnings run at a normal rate in every scenario; S0 checks they aren't flagged |
| noise-only | S0 | background errors only, `root_cause: null` |
| no-root-cause | S0; S4 for the Log agent | S0 is healthy. In S4 the root cause (ImagePullBackOff) isn't in the logs: the Log agent reports only symptoms (`capacity_degraded`) |

**Deferred: an auth-failure spike that is not an incident (S6).** Today the Log agent treats any WARN or ERROR pattern at ≥5× its baseline as anomalous, so a burst of failed logins would be reported as `success`, which counts as a false positive. That scenario needs agent-side support first (for example, treating client errors such as 401 separately from service errors), so it isn't seeded yet.
