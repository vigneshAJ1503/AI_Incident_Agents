# ADR-0020: AI observability and cost controls

- **Status:** Accepted
- **Date:** 2026-09-29

## Context
PR-041 has to make cost and latency visible per investigation and per agent, and add
controls (budgets, caching) without breaking three constraints: zero cost by default (Groq
free tier, no paid backend), a lean memory budget, and portability by configuration only
(ADR-0011). `TokenUsage` already flowed from each LLM response through the agents, planner
and RCA into `Investigation.usage`, but nothing priced it, nothing traced it, and the only
metrics were in the sample services.

## Decision
- **Price at the LLM boundary; extend `TokenUsage`.** One wrapper, `InstrumentedLLM`
  (`aiops.observability.llm`), is applied to every provider the orchestrator and the chat
  classifier use. It sets `usage.cost_usd` from the profile's `cost.pricing` table (model id >
  provider host > `*`, unlisted = $0) at the price of the model that answered. `TokenUsage`
  gains `cost_usd` and sums it like the tokens, so the cost reaches `AgentResult.usage`,
  `Investigation.usage`, the store, the API and the UI through the paths tokens already use.
  No parallel cost ledger in the data model. `evals.pricing` is kept as a deprecated alias.
- **OpenTelemetry API, SDK only when configured.** Spans (`investigation` > `plan` /
  `invoke_agent {agent}` / `rca` > `chat {model}` / `execute_tool {tool}`) use the GenAI
  semantic conventions plus `aiops.*` attributes. Without `observability.tracing.
  otlp_endpoint` (or `OTEL_EXPORTER_OTLP_ENDPOINT`) the OTel API's no-op tracer is used:
  nothing is exported, nothing listens. Prompts, tool arguments and outputs are never span
  attributes (they can hold customer data). The trace backend is any OTLP/HTTP collector;
  locally an opt-in Jaeger v2 (in-memory, 128 MiB limit, `make tracing-up`), never part of
  `make demo`.
- **Prometheus `/metrics` on the API, bounded labels.** `prometheus_client` counters and
  histograms labelled only by agent, model role, configured model, capability, allowlisted
  tool, mode and status. Investigation ids never become labels (they are trace attributes).
  A tool name outside the allowlist is recorded as `_unlisted`. Bounded series are created at
  0 on startup so the first event after a scrape is visible to `increase()`. `/metrics` sits
  outside `/api` (no API key, like a liveness probe) and the Web UI proxy never forwards it.
- **Human override rate = denied / (approved + denied)** approval decisions, as a PromQL
  ratio. There is no "edit" action in the approval flow yet, so edits are not counted.
- **Budgets end in the deterministic analysis, not in PARTIAL.** When an agent's
  `max_tokens`, or its investigation's live ledger (`orchestrator.max_tokens`, new
  `max_cost_usd`), is spent, the agent finishes through the existing zero-LLM fallback (the
  same path as an LLM outage) and a note says so. The ledger is updated on every LLM call,
  so agents running in parallel stop when the investigation budget is gone, not only the
  steps that start later.
- **Tool cache per investigation, not per process.** The orchestrator owns a short-TTL LRU
  cache (`orchestrator.tool_cache_ttl_s`, default 120 s) that agent (read-only) toolsets use;
  the approval executor's write toolset never does. Keys = investigation id + capability +
  tool + canonical arguments; only successful results are cached; a hit is still an audited
  `ToolCall` with `cached: true` that counts against `max_tool_calls`.
- **Model routing stays role-based.** `fast | agent | rca` -> `llm.models` is unchanged; the
  role and the model it resolved to are on every LLM span and metric.

## Consequences
- Cost is visible end to end at $0 on the free tiers; a company sets its prices in its
  profile and gets real numbers plus a cost cap, with no code change.
- New dependencies: `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http` (the API
  was already a transitive dependency of the MCP SDK, whose client spans now join our
  traces) and `prometheus-client`.
- The replay regression gate is unchanged: replays spend zero tokens and have no duplicate
  tool calls, so neither the pricing nor the cache changes a score.
- The `agent_finished` SSE event, `TokenUsage`, `ToolCall` and the dashboard summary gained
  additive fields (`cost_usd`, `cached`, `cost`).
