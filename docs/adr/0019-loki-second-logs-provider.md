# ADR-0019: Loki as the second logs provider (the portability proof)

- **Status:** Accepted
- **Date:** 2026-09-28

## Context
The human's first requirement is that switching a vendor is **configuration + one provider
adapter, with zero agent/orchestrator change** (ADR-0011/0012). PR-P4 had to prove it with a
second logs backend on the real cluster, reaching the same scenario ground truth as
Elasticsearch. The Log agent asks four fixed questions (volume by level, patterns with
first/last seen, versions/startups, first occurrences) and expects one tabular answer per
question with a `window` (current | baseline) column.

## Decision
- **Loki** (not OpenSearch): a different query language and data model (label-indexed
  streams, query-time parsing) makes a stronger proof than an Elasticsearch fork, and it is
  the most common "other" logs stack. `grafana/loki:3.7.8` single binary, filesystem storage,
  2-day retention, opt-in (`make loki-up`), ≤256 MiB.
- **Same pipeline, dual output:** Fluent Bit's existing Elasticsearch output is unchanged;
  a second `loki` output ships the same records. Stream labels are low-cardinality only
  (`namespace`, `app`, `level`); everything else stays in the JSON line.
- **Our own `loki-mcp`** (like elasticsearch-mcp/prometheus-mcp): four generic read-only
  tools (`query`, `query_range`, `list_labels`, `label_values`); guardrails server-side,
  including a **stream-selector allowlist** (every `{...}` must contain an allowlisted exact
  matcher), checked by a string-aware scanner so regexes/templates can't hide selectors.
- **One LogQL call per agent question** (the adapter contract stays one `ToolRequest` per
  question):
  - the incident/baseline split is a query-time `label_format window=` template on
    `unixEpochMillis __timestamp__`;
  - several statistics come back from one instant query as `or`-joined vectors tagged by
    `label_replace(..., "stat", "<name>", "", "")`; the adapter's `table()` pivots them;
  - first/last seen = `min/max_over_time` of an unwrapped epoch-ms label (exact to the ms).
- **Enforced proof:** `test_portability_proof.py` checks, from git history, that the change
  which added `providers/logs/loki.py` touched no file under `agents/` or `orchestrator/`
  (full-history checkout in CI); replay tests run the recorded Loki fixtures through the
  unchanged agent against the same scenario expectations as the Elasticsearch fixtures.

## Alternatives considered
- **Several tool calls per question** (e.g. one `query_range` per statistic, merged in the
  adapter): would need an interface change (the agent makes exactly one call per question)
  and more evidence rows; rejected to keep the agent untouched.
- **Fetching raw lines and aggregating in Python:** bounded by Loki's line limits, wrong
  counts on busy services; metric queries count everything server-side.
- **High-cardinality labels (`version`, `trace_id`) or structured metadata for fields:**
  faster filters, but against Loki's cardinality guidance; `| json` at query time is fine at
  this scale.
- **A community Loki MCP server:** no server-side stream allowlist or range caps, and the
  project keeps one guarded pattern for every capability (ADR-0003/0009).

## Consequences
- `profiles/local-loki` differs from `local-k8s` only in `capabilities.logs` and the logs
  identifier in `services.yaml` (`aiops profile diff local-k8s local-loki`).
- The instant queries scan `[baseline start, end]`; with a 24 h baseline on a large tenant
  they cost more than an index-backed ES|QL aggregation. Loki's own limits
  (`max_query_series`, `query_timeout`) and loki-mcp's caps bound them.
- `aiops doctor` has a Loki smoke test (`list_labels` + a 1-hour count per sampled service).
