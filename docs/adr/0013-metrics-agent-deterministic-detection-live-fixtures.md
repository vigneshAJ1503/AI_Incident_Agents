# ADR-0013: Metrics agent: deterministic anomaly detection from settings-built PromQL, live-recorded fixtures

- **Status:** Accepted
- **Date:** 2026-09-28

## Context
UC-05 asks the Metrics agent to say *what* changed, *when* and *by how much* (e.g. S1: p95 ≥ 5×
and the 5xx jump, with correct start times). LLMs are unreliable at arithmetic over time series,
and the project must move between companies by configuration only (another company's metric and
label names, another Prometheus-compatible backend). Unlike logs and alerts, real metrics can't be
seeded at a fixed time: they only exist while a real fault runs in Minikube.

## Decision
- **PromQL library built from `capabilities.metrics.settings`** (`metrics_agent/promql.py`;
  since PR-P2c the `metrics/prometheus` provider adapter, ADR-0012):
  metric and label names, error-status regex, rate window, step, baseline length and link
  templates are settings with defaults; label values come from the service catalog. Each query
  groups by the service label and covers the service **and its catalog dependencies** in one
  `query_range` (10 calls). Plain PromQL over the HTTP API, so Prometheus, Thanos, Mimir /
  Grafana Cloud and VictoriaMetrics all work.
- **Deterministic detection in Python** (`analysis.py`): baseline = median of the
  `baseline_minutes` before the window; per-metric unit-based rules (absolute delta and ratio,
  or a level); the change point is the first point of a sustained run (3 of 4 points at 30 s);
  magnitude = median/peak during the anomaly, ratio, z-score. No baseline data = never an anomaly.
- **Signals are data-derived and authoritative** in `finalize` (the LLM can neither invent nor
  hide an anomaly); metrics are symptoms, so a root-cause FACT is downgraded to a HYPOTHESIS.
- **Fixtures are recorded from live faults** (`aiops fault run S<n> -- record_metrics`), ~6 min
  after injection, with the window in `meta.json`; the replay runner rebuilds the task from it.
  S0 is recorded with no fault while holding the cluster lock.

## Consequences
- Zero-token replay evals score real Prometheus behaviour (rate windows, histogram buckets,
  restarts), not hand-written series.
- Fixtures depend on when they were recorded; re-recording needs ~30 min of quiet cluster
  (baseline + window) and only one fault injector at a time.
- No CPU-saturation signal: cAdvisor isn't scraped (read-only stack, PR-020); DB pool saturation
  and process RSS cover saturation and memory pressure.
- Thresholds live in code as unit-based defaults; making them per-company settings is a
  follow-up if a company needs different sensitivity.
