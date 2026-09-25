# Incident scenarios

Each scenario is a reproducible incident with **ground truth**, used for agent tests and evals (MASTER_PLAN.md §6, §15).

```
scenarios/<id>/
├── README.md            # what happens, what the investigation should conclude
├── expected.yaml        # question, service, window, ground-truth root cause
└── agents/<agent>.yaml  # expectations for one agent (status, signals, ...)
```

- **Synthetic data:** PR-007 seeds log data for each scenario (`make seed-logs S=S1`); PR-023 seeds the matching Alertmanager alerts (`make seed-alerts S=S1`, see `docs/setup/alerts.md`).
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
