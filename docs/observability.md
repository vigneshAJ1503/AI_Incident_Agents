# AI observability and cost controls (PR-041)

This page covers what the platform measures about **itself**: every investigation, agent, LLM
call and tool call, what it cost, and the controls that keep the cost bounded. The design is
in [ADR-0020](adr/0020-ai-observability-and-cost-controls.md).

Everything here is zero-cost and on by default, except tracing. Tracing is opt-in because it
needs a backend.

| What | Where | Default |
| --- | --- | --- |
| Cost per investigation and per agent | API (`usage.cost_usd`), Web UI report + dashboard, CLI | on ($0 on free tiers) |
| Prometheus metrics | the API's `GET /metrics` | on |
| Grafana dashboard "AI Observability" | `make prometheus-up grafana-up` | opt-in UI |
| OpenTelemetry traces | any OTLP/HTTP collector; locally Jaeger (`make tracing-up`) | **off** |
| Budgets per agent and per investigation | profile `agents.*.limits`, `orchestrator.*` | on |
| Tool cache per investigation | profile `orchestrator.tool_cache_ttl_s` | on (120 s) |

## Cost

Every LLM call is priced with the profile's `cost.pricing` table, in USD per 1M tokens:

```yaml
cost:
  pricing:
    api.groq.com: { input: 0.0, output: 0.0 }   # Groq free tier: $0
    "*": { input: 0.0, output: 0.0 }
    # your-agent-model: { input: 3.00, output: 15.00 }   # a company's contract prices
```

- **Lookup order:** the model id that answered, then the provider host, then `*`. A model
  that isn't listed costs $0.
- **Company prices:** a company overrides the table in its own profile. No code changes.
  The deprecated `evals.pricing` is still read, but `cost.pricing` wins per key.
- **Where the cost goes:** it is stored on the existing `TokenUsage` as `cost_usd`, so it
  appears everywhere tokens do:
  - each agent result's `usage`;
  - the investigation's `usage` (agents + planner + RCA);
  - the `agent_finished` SSE event;
  - the dashboard summary (`agents[].cost_usd`, `avg_cost_usd`, `cost.total_usd`,
    `cost.avg_usd_per_investigation`);
  - `aiops investigate` (a Tokens and a Cost column);
  - the scorecard of `aiops evaluate`.
- **Web UI:** the report page has a **Cost & tokens** card per agent (planner + RCA is shown
  as one row). The dashboard has a **Cost by agent** card.

## Budgets (cost controls)

| Budget | Setting | When it is spent |
| --- | --- | --- |
| Per agent (tokens) | `limits.max_tokens`, `agents.<name>.limits.max_tokens` | The agent **finishes with its deterministic analysis**, the same zero-LLM path used in replay or when the LLM fails. The result says so: *"Token budget of the agent (40000) spent: finished with the deterministic analysis."* |
| Per investigation (tokens) | `orchestrator.max_tokens` (200 000) | Steps that haven't started are **skipped** (PARTIAL, with a recoverable `error` event). Agents that are already running finish deterministically at their next LLM turn. |
| Per investigation (cost) | `orchestrator.max_cost_usd` (0 = off) | Same as the token budget, measured in USD from `cost.pricing`. |

Before PR-041, the investigation budget was only checked when a step started, against the
usage of *finished* agents. Now a live ledger records every LLM call, so four agents running
in parallel stop together.

## Tool cache

Agents in the same investigation often ask the same question twice. For example, a round-2
follow-up repeats a round-1 query, or two agents list the same alerts. The orchestrator gives
each investigation a short-lived cache:

- **What is cached:** identical calls, meaning the same capability, tool and canonical
  arguments. Only successful results are cached.
- **Settings:** `orchestrator.tool_cache_ttl_s` (default 120 s; 0 turns the cache off) and
  `tool_cache_max_entries` (LRU, default 512).
- **Read-only only:** agent toolsets are read-only by construction. The approval executor's
  write toolset never uses the cache.
- **A hit is still audited:** it is recorded as a `ToolCall` with `cached: true`, and it still
  counts against the agent's `max_tool_calls`.
- **Scope:** the cache is per investigation and is dropped when the investigation ends.
  Nothing is shared between investigations.

The recorded replays S0–S5 contain no duplicate calls, so the cache changes no eval score.
It pays off in live mode, where LLM follow-ups repeat queries.

## Model routing

Routing stays role-based: `llm.models.{fast, agent, rca}`.

| Role | Used by |
| --- | --- |
| `fast` | the planner fallback and the chat classifier |
| `agent` | the agents (`agents.<name>.model_role`) |
| `rca` | the RCA ranking |

Every LLM span and metric carries both the **role** and the **model** it resolved to, so
the dashboard shows which model answers which role, how fast and at what cost.

## Prometheus metrics (`GET /metrics`)

The endpoint is served by the API outside `/api`, so it needs no API key, like a liveness
probe. The Web UI's `/api/*` proxy never forwards it. You can turn it off with
`observability.metrics.enabled: false`. To dump it from a running `make api`, run
`make ai-metrics`.

| Metric | Labels | What |
| --- | --- | --- |
| `aiops_investigations_total` | mode, status | finished investigations |
| `aiops_investigation_duration_seconds` | mode | histogram |
| `aiops_investigation_cost_usd` | mode | histogram of the estimated cost per investigation |
| `aiops_investigation_confidence` | mode | report confidence (investigations that named a root cause) |
| `aiops_agent_runs_total` | agent, status | success / no_signal / partial / failed |
| `aiops_agent_duration_seconds` | agent | histogram |
| `aiops_agent_confidence` | agent | histogram |
| `aiops_agent_cost_usd_total` | agent | estimated cost of agent runs |
| `aiops_agent_fallbacks_total` | agent, reason | deterministic finish: `llm_error`, `agent_budget`, `investigation_budget` |
| `aiops_budget_skipped_steps_total` | agent | steps skipped by the investigation budget |
| `aiops_llm_requests_total` | agent, role, model, status | `ok`, `error`, `rate_limited`; `agent` = the caller (an agent, `planner`, `rca`, `intent`) |
| `aiops_llm_request_duration_seconds` | role, model | histogram |
| `aiops_llm_tokens_total` | agent, role, model, direction | `input` / `output` |
| `aiops_llm_cost_usd_total` | agent, role, model | estimated cost |
| `aiops_tool_calls_total` | capability, tool, status, cached | tool calls and cache hits |
| `aiops_tool_call_duration_seconds` | capability, tool | histogram (uncached calls only) |
| `aiops_approval_decisions_total` | action, decision | `approved` / `denied` by a human |

**The human override rate** is `denied / (approved + denied)`. The approval flow has no
"edit" action yet, so edits aren't counted. If one is added, it becomes a third `decision`
value.

**Cardinality is bounded.** Every label value comes from the profile or the agent registry:
agent names, the three model roles, configured model ids, capabilities, allowlisted tool
names, modes and statuses.

- Investigation ids, questions and services are **never** labels. They are trace
  attributes.
- A tool name outside the allowlist (for example, one a model invented) is recorded as
  `_unlisted`.
- Bounded series are created at 0 on startup, so `increase()` sees the first event.

### Prometheus and Grafana

The Prometheus scrape job `aiops-api` (`deploy/compose/config/prometheus/prometheus.yml`)
has two targets, and whichever is running reports `up`:

- `aiops-api:8000`: the container, for `make demo` / `make api-up`;
- `host.docker.internal:8000`: `make api` on the host.

The provisioned dashboard **AI Observability** (`uid aiops-ai-observability`) is in the
"AI Incident Agents" folder. It has these panels:

- **Headline numbers:** investigations, estimated cost, average cost per investigation,
  tokens, human override rate and average report confidence;
- **Latency:** agent latency p50/p95 by agent, LLM latency p95 by role and model, and tool
  latency p95 by capability;
- **Errors:** agent, LLM and tool errors;
- **Spend:** tokens per minute by model, and cost by agent;
- **Quality and controls:** agent confidence, budget fallbacks and skips, the tool cache hit
  ratio, requests by role and model, and agent runs by status.

```bash
make prometheus-up grafana-up   # ~0.1 GB + ~0.25 GB; opt-in, not part of make demo
make api                        # or make demo / make api-up
open http://localhost:3000/d/aiops-ai-observability
```

## Traces (OpenTelemetry, opt-in)

Tracing is **off by default**. Nothing is exported and no extra process runs; the code uses
the OpenTelemetry API's no-op tracer. To turn it on, point the profile at any **OTLP/HTTP**
collector, such as Jaeger, Grafana Tempo, Honeycomb, a Datadog agent, or an OpenTelemetry
Collector:

```yaml
observability:
  tracing:
    otlp_endpoint: ${AIOPS_OTLP_ENDPOINT:-}   # e.g. http://localhost:4318 ("/v1/traces" appended)
    service_name: aiops
    sample_ratio: 1.0                           # parent-based: whole investigations are kept
```

If `AIOPS_OTLP_ENDPOINT` is empty, the standard `OTEL_EXPORTER_OTLP_ENDPOINT` is honoured.

**One trace per investigation:**

```
investigation                          aiops.investigation.id, mode, profile, status, tokens, cost, confidence
├── plan                               service, steps, tokens
├── invoke_agent logs                  gen_ai.agent.name, aiops.llm.role, round, tokens, cost, model, status
│   ├── execute_tool execute_esql      aiops.capability, cached, result_chars, status
│   │   └── (MCP client span)          the MCP SDK's own span; the context propagates to the server
│   └── chat <model>                   gen_ai.request.model, response.model, usage.input/output_tokens, cost, role
├── invoke_agent metrics ...
└── rca
    └── chat <rca model>
```

Attribute names follow the OpenTelemetry **GenAI semantic conventions** where they exist.
**Prompts, tool arguments and tool output are never recorded**, because they can contain
customer data.

### Local trace backend: Jaeger (opt-in)

```bash
make tracing-up                                  # Jaeger v2 2.21.0: UI :16686, OTLP/HTTP :4318
export AIOPS_OTLP_ENDPOINT=http://localhost:4318
make api                                         # or: cd backend && uv run aiops investigate --replay S1
open http://localhost:16686                      # service "aiops"
make tracing-down
```

For the API **container** (`make demo`, `make api-up`), set
`AIOPS_OTLP_ENDPOINT_IN_NETWORK=http://aiops-jaeger:4318` in `.env`.

Jaeger runs with an in-memory store capped at 2000 traces, `mem_limit: 128m` and a read-only
filesystem, and it listens only on 127.0.0.1. **Measured: 16–17 MiB** after 10 replayed
investigations (56 spans each). It is never part of `make demo`.
