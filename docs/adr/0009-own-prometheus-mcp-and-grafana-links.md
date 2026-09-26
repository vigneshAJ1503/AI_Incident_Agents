# ADR-0009: Our own thin read-only Prometheus MCP, and Grafana deep links instead of a Grafana MCP

- **Status:** Accepted
- **Date:** 2026-09-26

## Context
The Metrics agent (UC-05, PR-022) runs about ten PromQL range queries per investigation (error
ratio, p95/p99 latency, throughput, DB pool, cache, memory and restarts, over the incident window
plus a baseline). It also needs discovery (metric names, metadata) and a check that the data is
being collected at all (scrape targets). MASTER_PLAN §16 requires read-only access, a
per-capability tool allowlist and guardrails enforced **server-side**. §9 had pencilled in
`ghcr.io/pab1it0/prometheus-mcp-server` for Prometheus, plus the official Grafana MCP for panel links.

**Evaluated: `pab1it0/prometheus-mcp-server` v1.6.2** (Python/FastMCP, image
`ghcr.io/pab1it0/prometheus-mcp-server:1.6.2`, ~80 MB, runs as uid 1000):
- **Tools:** `health_check`, `execute_query`, `execute_range_query`, `list_metrics`,
  `get_metric_metadata`, `get_targets`. It is read-only by construction and has streamable HTTP.
- **Memory:** ~68 MiB idle (measured with `docker stats`).
- **Guardrails:** only a request timeout (`PROMETHEUS_REQUEST_TIMEOUT`) and optional pagination.
  No limit on range, step, points or series, and no metric allowlist. Measured: a 56-day
  `execute_range_query` of `{__name__=~".+"}` at `step=1s` is forwarded as-is. Only Prometheus's
  own 11,000-points check rejects it. A 1-day query of the same selector at a legal step would run
  against the whole TSDB. Results are passed through untrimmed.

**Grafana MCP (`grafana/mcp-grafana`)** would only produce links. It needs a running Grafana plus a
service-account token, and costs another container (~50–100 MB). Locally, Grafana is opt-in
(`make ui-up`) to stay inside the 5 GB budget, so agents can't depend on it.

## Decision
1. **Build `mcp-servers/prometheus-mcp`** (MCP SDK v2 `MCPServer` + `httpx2`, GET-only against
   the Prometheus HTTP API v1). It has five tools: `query`, `query_range`, `list_metrics`,
   `metric_metadata`, `get_targets`. Every check runs server-side, before Prometheus is contacted:
   - `query_range` span ≤ `MAX_RANGE_HOURS` (24). The same bound applies to every `[range]`
     selector, subquery and `offset` inside a query.
   - step ≥ `MIN_STEP_S` (15) and ≤ `MAX_POINTS` (1,100) points per series. An omitted step is
     auto-chosen as the finest step that fits.
   - ≤ `MAX_SERIES` (50) series per result (sorted, then capped, reported as `truncated`);
     `MAX_QUERY_LENGTH`; no control characters.
   - **No whole-TSDB scans:** every selector must name its metric. Bare `{…}` selectors and
     `__name__` matchers are rejected.
   - An optional **metric allowlist** (`METRIC_ALLOWLIST`, regexes). Locally it is set to the
     metrics the agents use.
   - `QUERY_TIMEOUT_S` is sent to Prometheus as `timeout=` (Prometheus aborts the evaluation) and
     bounds the HTTP call. Prometheus's `--query.max-samples` stays the last line of defence.
   - Compact, deterministic results: rounded values, NaN/Inf → `null`, epoch-second timestamps.
     This keeps recorded fixtures small and stable.
   It runs in a container: non-root uid 10001, read-only root FS, 127.0.0.1:8103, `mem_limit: 128m`
   (~52 MiB in use).
2. **No Grafana MCP.** Agents build **deep links from templates** in the capability settings
   (`capabilities.metrics.settings.ui_link_template` for a Grafana dashboard panel,
   `explore_link_template` for the exact PromQL in the always-on Prometheus UI), using the shared
   helper `aiops.core.links`. Dashboard uids and panel ids are stable because the dashboards are
   provisioned from git.

## Consequences
- The agent's tool names are ours and stable. A company can still point the `metrics` capability
  at `pab1it0/prometheus-mcp-server` (or Grafana's MCP, which also has Prometheus tools), but must
  map the tool names in the allowlist and accept weaker guardrails.
- **Any Prometheus-compatible API works unchanged**, e.g. Thanos, Mimir, VictoriaMetrics or
  Grafana Cloud: set `METRICS_PROM_URL` and `PROM_BEARER_TOKEN`/basic auth. Metric and label
  names are portability settings (`capabilities.metrics.settings.metrics` / `labels`), and
  per-service label values come from the service catalog (`metrics: {labels: {...}}`).
- **Companies that do run Grafana** can plug in `grafana/mcp-grafana` as an additional capability
  (e.g. `dashboards`) to search dashboards or render panels. Nothing in the Metrics agent depends
  on it. Links keep working either way because they are only strings.
- The PromQL lexer is deliberately small, not a full parser. It errs on the side of rejecting
  (unknown shapes without a metric name are refused), and Prometheus parses the query anyway.
- One more small server to maintain: ~600 lines plus 26 unit tests and a live contract test
  (`backend/tests/integration/test_metrics_mcp.py`).
