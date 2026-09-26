# Real log shipping: Fluent Bit → Elasticsearch (PR-016)

The Log agent was built on synthetic logs (`aiops seed logs`, indices `payment-prod-*` …).
PR-016 ships the **real** logs of the sample services running in Minikube into
Elasticsearch, and switches the Log agent to them **by configuration only**
(MASTER_PLAN D12, the early portability proof).

## Architecture

```
Minikube node "aiops"                                           Docker network "aiops"
┌───────────────────────────────────────────────────────┐     ┌──────────────────────────┐
│ namespace prod: payment/order/user/inventory, ...     │     │ aiops-elasticsearch:9200 │
│   stdout (JSON) -> /var/log/containers/*_prod_*.log   │     │   logs-k8s-YYYY.MM.DD    │
│                                                       │     │   template aiops-k8s-logs│
│ namespace logging: DaemonSet fluent-bit (5.1.2)       │     │   ILM: delete after 2d   │
│   tail (CRI) -> kubernetes -> parser -> modify -> es ─┼────►│                          │
└───────────────────────────────────────────────────────┘     └────────────▲─────────────┘
                                                                           │ ES|QL (read-only,
             AIOPS_ENV=local-k8s  aiops agent run logs ...  ── elasticsearch-mcp ─┘ allowlist logs-k8s-*)
```

Pipeline (`deploy/k8s/logging/fluent-bit.conf`):

1. **tail** `/var/log/containers/*_prod_*.log` with the `cri` multiline parser (containerd).
   Only namespace `prod` is shipped: kube-system noise would cost Elasticsearch heap.
   Offsets live in a small SQLite DB on the node (`/var/lib/aiops-fluent-bit`), so a
   Fluent Bit restart neither re-ships nor skips lines.
2. **kubernetes** adds `kubernetes.{namespace_name, pod_name, container_name, container_image, host, labels.*}`
   (read-only ServiceAccount: `get/list/watch` on pods and namespaces, nothing else).
3. **parser** (`app_json`) lifts the app's JSON line to the **top level**, so a document
   has exactly the synthetic fields: `@timestamp` (the app's own), `level`, `message`,
   `service`, `environment`, `version`, `host`, `logger`, `trace_id`, `http_method`,
   `endpoint`, `status_code`, `duration_ms`, `error_type`. Non-JSON lines (Postgres,
   Redis) keep their raw text in `log`.
4. **modify** drops the CRI envelope (`_p`, `time`, `stream`).
5. **es** → `http://aiops-elasticsearch:9200`, `Logstash_Format On`, prefix `logs-k8s`
   (daily index from the app timestamp), `Suppress_Type_Name On`, `Replace_Dots On`.

A shipped document:

```json
{ "@timestamp": "2026-09-26T08:38:47.372Z", "level": "INFO",
  "message": "POST /api/v1/pay completed with 200 in 808ms",
  "service": "payment-service", "environment": "production", "version": "v1.8.1",
  "host": "payment-service-847d4fc6f9-dtmf8", "logger": "com.acme.payment.PaymentController",
  "trace_id": "27274483b6ccb0f7", "http_method": "POST", "endpoint": "/api/v1/pay",
  "status_code": 200, "duration_ms": 808,
  "kubernetes": { "namespace_name": "prod", "pod_name": "payment-service-847d4fc6f9-dtmf8",
                  "container_name": "app", "labels": { "app": "payment-service", "team": "payments" }, "...": "..." } }
```

### Index template and retention

`aiops seed k8s-logging` (run by `make logging-up`) PUTs, idempotently:

- the index template **`aiops-k8s-logs`** for `logs-k8s-*`: the same app-field mappings as
  the synthetic template (`aiops/seed/logs.py`: keywords, `message` as text +
  `message.keyword`), `kubernetes.*` keywords (labels via a dynamic template),
  `dynamic: false` elsewhere, 1 shard, 0 replicas, `refresh_interval: 5s`.
  Its **priority is 200**: `logs-k8s-*` also matches Elasticsearch's built-in `logs-*-*`
  **data-stream** template (priority 100), which would otherwise turn the index into a data
  stream that Fluent Bit's bulk `index` requests can't write to.
- the ILM policy **`aiops-k8s-logs-retention`**: delete each daily index **2 days** after
  it was created (`--retention 1d` to be leaner). ES has a 512 MB heap; synthetic indices
  (`*-prod-*`) are untouched.

## Usage

```bash
make infra-up mcp-up k8s-up   # Elasticsearch + MCP servers + Minikube with the sample services
make logging-up               # template + ILM, then the Fluent Bit DaemonSet
make logging-status           # pod, memory, logs-k8s-* doc counts
make test-logging             # live: fresh cluster logs searchable within 30 s, agent works
make logging-down             # remove Fluent Bit (indices expire via ILM)
```

Investigate on real logs (no LLM key needed to see the deterministic phase: `LLM_PROVIDER=fake`):

```bash
cd backend
AIOPS_ENV=local-k8s uv run aiops agent run logs "Payment API is returning HTTP 500" -s payment --since 30m
```

## How the config switch works (zero agent code change)

`config/environments/local-k8s.yaml` **extends** `local` and overrides only the `logs`
capability; `config/service-catalog/local-k8s.yaml` **extends** the `local` catalog and
overrides only each service's production log index:

```yaml
# config/environments/local-k8s.yaml
extends: local
environment: local-k8s
service_catalog: local-k8s
capabilities:
  logs:
    settings:
      fields: { service: kubernetes.labels.app }   # field mapping: the pod's app label
      service_filter: true                         # one index for all services
      baseline_hours: 0.25                         # short-lived, shared cluster

# config/service-catalog/local-k8s.yaml
extends: local
services:
  - name: payment-service
    environments: { production: { capabilities: { logs: { index_pattern: "logs-k8s-*" } } } }
  # ... same for order/user/inventory
```

- `extends:` (environments and catalogs) is a generic loader feature: mappings are merged
  deeply, catalog services by `name`, and the child never inherits the parent's
  `environment` name. Anything added to `local.yaml` later is inherited automatically.
- **Shared index → service filter.** Synthetic logs have one index per service; real
  Kubernetes logs share one. With `service_filter: true`, every Log-agent query starts
  with `FROM <index> | WHERE <fields.service> == "<catalog logs.service_value>"`, the UI
  links get the same KQL clause, and the prompt tells the LLM to filter too. Off by
  default, so the synthetic `local` queries (and their recorded fixtures) are unchanged.
- The elasticsearch-mcp allowlist already contains `logs-k8s-*`.

To point the Log agent at another company's shared log index, the same three knobs apply:
the index pattern in the catalog, the field mapping, and `service_filter`.

## Proof on real incidents

Fixtures recorded live against the cluster (`backend/tests/fixtures/logs-k8s/<S>/`, window
in `meta.json`) with `aiops fault run` holding the cluster lock:

```bash
# from the MAIN clone (shared lock)
cd backend && uv run aiops fault run S1 -- uv --directory <repo>/backend run --no-sync \
  python -m tests.fixtures.record_logs_k8s --out <repo>/backend/tests/fixtures/logs-k8s/S1
# healthy baseline: the recorder takes the lock itself and refuses if a fault is active
uv --directory <repo>/backend run --no-sync python -m tests.fixtures.record_logs_k8s \
  --scenario S0 --lock-repo <main clone> --out <repo>/backend/tests/fixtures/logs-k8s/S0
```

`backend/tests/unit/test_log_agent_k8s.py` replays them with zero tokens and scores them
against the same scenario ground truth as the synthetic runs.

Windows: S0 uses the scenario's 30-minute window; an incident uses **10 minutes before the
injection → now** (`--lead-minutes`), like an engineer asking "what happened since 10:05?".
The cluster is shared with other agents' scenarios, so the recorder checks the service's
logs first and **refuses to record** (exit 3) if the window it is judged on is polluted:
any `ERROR` line or a non-base version (from `deploy/k8s/base`) in the S0 window or in the
10 minutes before an injection, or a non-base version (an earlier run of an incident) in
the 15-minute baseline before it. `--check-only` just reports it.

**Why a 15-minute baseline** (`baseline_hours: 0.25` in `local-k8s`): the synthetic setup
compares with 24 h, but this cluster is shared by several agents that inject S1–S5 all the
time. With a 1-hour baseline, another agent's recent S1 run was almost always inside it,
which hides the v1.8.2 deployment and makes the DB-timeout pattern "known", so a clean
recording never came. 15 minutes of healthy traffic (~2.6 k payment lines) is enough for
this lab; a company would use hours to days.

Recorded: S0 = 30 min healthy window, `no_errors`, 0 anomalies. S1 = 10:01:43–10:13:13 UTC,
45 `Database connection timeout` errors (new vs baseline), `v1.8.2` first seen at the
injection, signals `db_timeout_errors_up, error_rate_up, new_error_pattern, deployment_detected`.

## Measured cost (2026-09-26, 76 minutes of traffic, 16 samples every 5 min)

| What | Measured | How |
|------|----------|-----|
| Fluent Bit (node container) | **8.2–9.0 MB** steady; 20.6 MB peak while reading the backlog at start | `minikube ssh -- sudo crictl stats` (no metrics-server) |
| Fluent Bit limit / request | 64 Mi / 32 Mi | the 5 MB `Mem_Buf_Limit` pauses the input instead of growing (seen once, at start) |
| Elasticsearch container | 1.012 → 1.089 GiB (limit 1.25 GiB) | `docker stats`; the rise is mostly Lucene/page cache |
| Elasticsearch heap | 23–79 % of 512 MB, sawtooth (GC), no upward trend | `_nodes/stats/jvm` |
| Ingest volume | ~64 k docs/hour, ~17 MB/hour on disk (prod only, 6 pods) | `_cat/indices/logs-k8s-*` |
| Retention | ≤ 2 daily indices + today's ≈ **0.8 GB** disk worst case | ILM `aiops-k8s-logs-retention` |
| Shipping delay | < 10 s from log line to searchable (Flush 2 s + refresh 5 s) | `make test-logging` asserts < 30 s |

Heap is the constraint: ES had ~1.09 of 1.25 GiB used while several agents also used it.
If memory gets tight, shorten retention (`aiops seed k8s-logging --retention 1d`).

## Security notes

- Fluent Bit runs as **UID 0**: kubelet writes `/var/log/pods` as `root:root` (dirs 0750,
  files 0640), so no other UID can read them, and Kubernetes doesn't give non-root
  processes file capabilities. The blast radius is limited instead: **all capabilities
  dropped** (root can still read files it owns), `allowPrivilegeEscalation: false`,
  read-only root filesystem, `seccompProfile: RuntimeDefault`, `/var/log` mounted
  **read-only**; the only writable host path is its offset DB directory.
- RBAC: a dedicated ServiceAccount with `get/list/watch` on `pods` and `namespaces` only.
- Elasticsearch stays local (the Docker network), no credentials involved; the Log agent
  reads through elasticsearch-mcp's server-side guardrails (index allowlist, time range).
