"""AI observability + cost controls (PR-041, docs/observability.md).

* ``tracing``: OpenTelemetry spans investigation -> agent -> tool call / LLM call (no-op
  unless ``observability.tracing.otlp_endpoint`` is set);
* ``metrics``: Prometheus metrics served on the API's ``/metrics`` (bounded labels);
* ``pricing``: the per-model price table (``cost.pricing``) -> ``TokenUsage.cost_usd``;
* ``llm``: the LLM wrapper that prices, traces and measures every call;
* ``budget``: the live per-investigation token/cost ledger and the current LLM scope;
* ``cache``: the short-TTL cache for identical read-only tool calls of one investigation.
"""
