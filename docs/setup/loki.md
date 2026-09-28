# Second logs backend: Grafana Loki (PR-P4a)

The portability proof (MASTER_PLAN §14 "Portability", PR-P4a; ADR-0019): the **same** Log
agent investigates the **same** Kubernetes logs in **Grafana Loki** instead of Elasticsearch,
with **zero agent or orchestrator code change**. What changes is one profile
(`profiles/local-loki`), one provider adapter (`backend/src/aiops/providers/logs/loki.py`),
its prompt fragment and its MCP server (`mcp-servers/loki-mcp`).

## Architecture

```
Minikube node "aiops"                                      Docker network "aiops"
┌──────────────────────────────────────────────┐          ┌───────────────────────────────┐
│ namespace prod: payment/order/user/inventory │          │ aiops-elasticsearch:9200      │
│   stdout (JSON)                              │   es ───►│   logs-k8s-YYYY.MM.DD (2 days)│
│ namespace logging: DaemonSet fluent-bit      │          │                               │
│   tail -> kubernetes -> lua -> parser ->     │  loki ──►│ aiops-loki:3100 (3.7.8)       │
│   modify -> [es] + [loki]                    │          │   streams {namespace,app,level}│
└──────────────────────────────────────────────┘          │   filesystem, 2 days          │
                                                          └──────────▲────────────────────┘
       AIOPS_PROFILE=local-loki aiops agent run logs ...  ── loki-mcp :8110 ┘ LogQL, read-only,
                                                             streams allowlist namespace="prod"
```

- **Loki** (`deploy/compose/docker-compose.infra.yml`, compose profile `loki`, opt-in):
  `grafana/loki:3.7.8` single binary (`-target=all`), TSDB index + filesystem chunks in the
  `loki-data` volume, **2 days retention** (compactor), no results cache, 16 MB chunk cache,
  `GOMEMLIMIT=180MiB`, `mem_limit: 256m`, UID 10001, `127.0.0.1:3101` (3100 is the Web UI).
  Config: `deploy/compose/config/loki/loki.yaml`.
- **Fluent Bit dual output** (`deploy/k8s/logging/fluent-bit.conf`): the existing `es`
  output is unchanged; a second `loki` output ships the same records to
  `http://aiops-loki:3100`. **Low-cardinality stream labels only:** `namespace`
  (`kubernetes.namespace_name`), `app` (the pod's `app` label), `level` (the app's level).
  Everything else (`message`, `version`, `trace_id`, `endpoint`, `status_code`, ...) stays
  in the JSON line and is parsed at query time; the pod name travels as structured
  metadata (`pod`). The `kubernetes` object is dropped from the line. If Loki is down,
  the loki output retries 3 times and drops its chunks; Elasticsearch is unaffected.
- **loki-mcp** (`mcp-servers/loki-mcp`, our own, MCP SDK v2, `127.0.0.1:8110`): tools
  `query_range` (log lines or series), `query` (instant metric LogQL), `list_labels`,
  `label_values`. Server-side guardrails: every stream selector must carry an allowlisted
  exact matcher (`ALLOWED_STREAMS=namespace=prod`), bounded time range / `[range]` /
  `offset` (48 h), line/series/points caps, query length, timeouts. GET-only.
- **Grafana** (`make grafana-up`, optional) gets a provisioned `Loki logs` datasource
  (uid `loki`); the agent's evidence links open Grafana **Explore** with the exact LogQL
  and time range.

## Start it

```bash
make infra-up k8s-up logging-up     # the usual stack + Fluent Bit (now dual output)
make loki-up                        # Loki + loki-mcp (builds the image once)
make loki-status                    # ready, memory, the prod apps Loki has streams for
make doctor PROFILE=local-loki ARGS=--skip-llm
cd backend && AIOPS_PROFILE=local-loki uv run aiops agent run logs \
  "Payment API is returning HTTP 500" -s payment-service --since 20m
make loki-down                      # stop Loki + loki-mcp (data kept; 2-day retention)
```

Loki only holds logs shipped since it started (Fluent Bit keeps its tail offsets), so give
it `baseline_hours` (15 min in `local-k8s`) of history before investigating.

## How the Log agent's questions become LogQL

The agent asks the same four questions as on Elasticsearch (ADR-0012); the adapter answers
each with **one** LogQL call and returns the neutral `LogTable` columns:

| Question | LogQL (payment-service, abbreviated) |
|----------|--------------------------------------|
| volume by level, window vs baseline | `sum by (window, level) (count_over_time({namespace="prod", app="payment-service"} \| label_format window=… [Ns]))` |
| error/warn patterns + first/last seen | `topk(1000, sum by (window, level, msg) (count_over_time({…, level=~"^(?:ERROR\|…\|WARN)$"} \| json msg="message" \| … [Ns])))` `or` `min by (…) (min_over_time(… \| label_format ts_ms=… \| unwrap ts_ms [Ns]))` `or` `max by …` |
| versions + startups | `count_over_time`, `count_over_time(… \| msg=~"^Starting.*$")`, `min_over_time(unwrap ts_ms)` `by (window, version)` |
| first occurrences + trace ids | `query_range` forward, limit 5: `{…} \|= "Database connection timeout…" \| json msg="message", trace_id="trace_id", version="version" \| msg=~"^Database…"` |

- The **incident/baseline split** is a query-time label:
  ``label_format window=`{{ if ge (unixEpochMillis __timestamp__) "<start ms>" }}current{{ else }}baseline{{ end }}` ``
  (equal-length digit strings compare correctly as strings).
- **Several statistics in one call:** each is tagged with `label_replace(…, "stat", "count", "", "")`
  and joined with `or`; the adapter pivots the series back into rows. First/last seen are
  the min/max of the line timestamp (`unwrap` of an epoch-ms label), exact to the
  millisecond like Elasticsearch's `MIN(@timestamp)`.
- Non-JSON lines keep counting (`| drop __error__, __error_details__` after `| json`), like
  Elasticsearch documents without the field.
- LogQL label-filter regexes must match the **whole** value (a bare prefix matches
  nothing: `msg=~"^Database connection timeout"` finds no line, `...timeout.*"` does), so the
  adapter always writes explicit `^…$` / `^prefix.*` patterns. (The first live S1 recording
  caught this: its first-occurrence query returned no lines.)
- Settings (`profiles/local-loki/profile.yaml`): `stream_labels` (which field roles are stream
  labels), `fields` (role → label or JSON key), `ui_link_template` (`{panes}`, `{query}`,
  `{from_ms}`, `{to_ms}`), `grafana_datasource_uid`. The catalog's `logs.index_pattern` is
  the stream selector `{namespace="prod"}` (`profiles/local-loki/services.yaml`).

## The proof (verified live)

- Fixtures recorded **live on Loki** (`make record-logs-loki S=S1`, inside `aiops fault run`
  from the main clone; `S=S0` healthy under the cluster lock), windows in `meta.json`:
  `backend/tests/fixtures/logs-loki/S{0,1}`. After the regex-anchoring fix, S1 was
  re-queried over its recorded window (`record_logs_loki --rerun`, Loki keeps 2 days):
  identical volume/pattern/version results, plus the first occurrence and trace ids.
- `backend/tests/unit/test_log_agent_loki.py` replays them with `AIOPS_PROFILE=local-loki`:
  S1 reaches `db_timeout_errors_up`, `error_rate_up`, `new_error_pattern`,
  `deployment_detected` (v1.8.2), the first occurrence with trace ids, a Grafana Explore
  link; S0 has no false positive; both reach the **same conclusions** as the Elasticsearch
  `local-k8s` fixtures.
- `backend/tests/unit/test_portability_proof.py` asserts from git history that the change
  which added the Loki adapter touched nothing under `backend/src/aiops/agents/` or
  `backend/src/aiops/orchestrator/` (CI checks out full history).

## Memory (measured, `docker stats` / `kubectl top`)

| Component | Measured | Limit |
|-----------|----------|-------|
| Loki (a few hours of `prod` logs, during agent queries) | 83–161 MiB | 256 MiB |
| loki-mcp | 55–59 MiB | 128 MiB |
| Fluent Bit (ES + Loki outputs) | 9–27 MB | 64 Mi |
| Whole stack (Minikube, ES, Prometheus, Alertmanager, Redis, Loki, 8 MCP servers, demo app) | ≈ 3.3 GiB | 5 GB budget |

## Grafana Cloud / another Loki

Point loki-mcp at it (`LOKI_URL`, `LOKI_USERNAME` = instance id + `LOKI_PASSWORD` = a
`logs:read` token, or `LOKI_BEARER_TOKEN`; `LOKI_ORG_ID` for multi-tenant Loki), set
`ALLOWED_STREAMS` to the company's selector (e.g. `cluster=prod-eu`), and map the company's
labels and JSON keys in `capabilities.logs.settings` (`stream_labels`, `fields`). No code.
