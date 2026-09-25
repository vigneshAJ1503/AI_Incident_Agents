# Alert Agent — UC-06

`aiops agent run alerts "Payment API is returning HTTP 500" -s payment-service`

Checks which alerts are firing for a service and its dependencies, whether any are muted, and when they started relative to the incident. Package: `backend/src/aiops/agents/alert_agent/`
(agent `alerts`, capability `alerts`, evidence kind `alert`, prompt `config/prompts/alerts/v1.md`).

**Alerts are evidence, never an automatic root cause.** `HighErrorRate` says errors are high, not why. The agent reports alerts as FACT/OBSERVATION/CORRELATION findings. A FACT typed `root_cause` is downgraded to HYPOTHESIS, and the RCA agent (PR-031) weighs alerts against the other sources.

## How it works
1. **Scope (deterministic).**
   - The service's alert labels come from the service catalog (`alerts: {labels: {service, namespace}}`, per environment).
   - Each `depends_on` service is also looked up in the catalog. Components that aren't in the catalog (`postgres`, `redis`) are matched as `service=<name>`.
   - Label names, critical severities and the Alertmanager UI link template come from `capabilities.alerts.settings`.
2. **Tool calls (1 + number of dependencies + 1).** `list_alerts` (state `all`) for the service and for each dependency, then `list_silences` for the service. Each call is evidence with a deterministic summary, a UI link filtered by the labels, and the earliest start time.
3. **Correlation (deterministic).**
   - An alert **fires** when it is active (not silenced or inhibited) and had started by the end of the window. Alerts that start after the window are listed separately.
   - Each start time is compared with the incident start. That is the window start, or a `hints.incident_start` / `hints.first_error_at` inside the window (e.g. the Log agent's first-seen time from the orchestrator).
   - The overview names the first alert to fire. Firing order is not causal order: rules have different `for` durations.
4. **Signals (all data-derived):**

   | Signal | Rule |
   |--------|------|
   | `alerts_firing` | an alert fires on the service itself |
   | `critical_alert_firing` | a firing alert (service or dependency) has a severity in `critical_severities` |
   | `dependency_alert_firing` | an alert fires on a `depends_on` service |
   | `alert_precedes_incident` | a firing alert started before the incident start |
   | `no_active_alerts` | nothing fires on the service or its dependencies (never set if a query failed) |

5. **LLM (bounded).** It gets the overview plus all five read-only tools (`get_alert`, `get_alert_groups`, …) and writes the evidence-cited summary.
6. **Finalize.**
   - Signals and success/no_signal come from the data: the LLM can neither invent an alert (S0) nor hide one.
   - When nothing fires, the summary always states it explicitly ("No active alerts for payment-service or its dependencies (…): no alert threshold was breached."), as in the HLD example.

## Scenario ground truth (`scenarios/*/agents/alerts.yaml`)
| Scenario | Firing (seeded by `make seed-alerts`) | Expected signals |
|----------|---------------------------------------|------------------|
| S0 healthy | none | `no_active_alerts`, status `no_signal` |
| S1 DB pool | DatabaseConnectionPoolExhausted (10:11), HighErrorRate (10:12) on payment-service | `alerts_firing`, `critical_alert_firing` |
| S2 memory leak | HighErrorRate, PodOOMKilled, PodCrashLooping on order-service | `alerts_firing`, `critical_alert_firing` |
| S3 slow dependency | HighErrorRate + HighLatencyP95 on order-service, HighLatencyP95 on inventory-service (dependency) | + `dependency_alert_firing` |
| S4 bad deployment | DeploymentReplicasMismatch (warning) on user-service | `alerts_firing` only |
| S5 cache outage | RedisDown (dependency, critical), HighLatencyP95 on payment-service | `alerts_firing`, `critical_alert_firing`, `dependency_alert_firing` |

## Tests
- Fixtures: `backend/tests/fixtures/alerts/<scenario>/alerts.json`, recorded live through alertmanager-mcp after `aiops seed alerts --now 2026-09-25T10:30:00`. Re-record with `make record-fixtures-alerts`.
- Unit tests (zero tokens):
  - `tests/unit/test_alert_agent.py` covers replay S0–S5 via the eval runner, plus guardrails, hints and failures
  - `tests/unit/test_alert_analysis.py`
  - `make eval AGENT=alerts`
- Integration:
  - `tests/integration/test_alerts_live.py` runs the live eval for S0–S5 (seed → MCP → agent)
  - `tests/integration/test_alerts_mcp.py`

## Known limits
- **No history:** Alertmanager returns only alerts that haven't ended, so resolved alerts are invisible until Prometheus `ALERTS` is connected (PR-020). The overview says so.
- **Seeded data:** alerts are synthetic until PR-020. Their names, labels and annotations match `deploy/compose/config/prometheus/alert-rules.yml`.
- **Correlation anchor:** without a hint, the incident start is the window start. The orchestrator (PR-030) passes the Log agent's first error time as a hint.
