# Metrics locally: Prometheus, kube-state-metrics and Grafana

The metrics slice (EPIC-007) runs on the **real** sample system in Minikube: real requests, real
latency, real pod restarts. Nothing here is synthetic.

| Piece | Where | Memory |
|-------|-------|--------|
| Prometheus `v3.15.0` on `127.0.0.1:9090` (always on, part of `make infra-up`) | `deploy/compose/docker-compose.infra.yml`, config in `deploy/compose/config/prometheus/prometheus.yml` | see below |
| kube-state-metrics `v2.20.0` in namespace `monitoring`, NodePort 30080 (part of `make k8s-up`) | `deploy/k8s/monitoring/` | inside Minikube's 2.2 GB |
| Grafana `13.2.2` on `127.0.0.1:3000` (**optional**, `make ui-up` or `make grafana-up`) | provisioning + dashboards in `deploy/compose/config/grafana/` | see below |

```bash
make infra-up k8s-up          # Prometheus comes with infra-up; kube-state-metrics with k8s-up
make monitoring-up            # (re)deploy kube-state-metrics only
open http://localhost:9090/targets     # 5 targets up: 4 sample services + kube-state-metrics
make grafana-up               # optional: http://localhost:3000 (anonymous, read-only)
make ui-down                  # stop Kibana + Grafana again
make check-rules              # promtool check config + rules + rule unit tests, amtool
```

## What is scraped

Every 30 s (the lean budget), nothing else:

| Job | Target | Series |
|-----|--------|--------|
| `sample-services` | `172.21.0.100:30081..30084/metrics` (payment, order, user, inventory) | `http_requests_total{service,team,namespace,endpoint,method,status}`, `http_request_duration_seconds_bucket{…,le}`, `db_pool_connections_{active,max,pending}`, `redis_up`, `process_resident_memory_bytes`, `process_cpu_seconds_total` |
| `kube-state-metrics` | `172.21.0.100:30080/metrics` | pods and deployments in `prod` only: restarts, last termination reason (`OOMKilled`), waiting reason (`ImagePullBackOff`), desired/available replicas, resource limits, `kube_pod_labels` / `kube_deployment_labels` with `label_app`, `label_team` |

The apps label their own series with `service`, `team` and `namespace` (the metric contract at the top of `alert-rules.yml`). The scrape config adds the same values as **target labels** only so the standard `process_*` series (which the apps don't label) can be joined to a service. `honor_labels: true` keeps the apps' own values whenever both exist.

kube-state-metrics runs with `--namespaces=prod --resources=pods,deployments` and `--metric-labels-allowlist=pods=[app,team],deployments=[app,team]`: the alert rules join on `label_app` / `label_team`. Its ClusterRole can only `list`/`watch` pods and deployments. CPU and memory *usage* from cAdvisor is deliberately not scraped: it would need a token with kubelet `nodes/proxy` access, which is too much power for a read-only stack. Process RSS from the apps covers memory pressure.

## Real alerts

Prometheus loads `deploy/compose/config/prometheus/alert-rules.yml` and sends to `aiops-alertmanager:9093`, so **alerts now fire from real metrics** during live faults. Measured with `aiops fault run SX -- <poller>` (times after the fault was injected):

| Scenario | Alerts that fired (service, time after injection) |
|----------|---------------------------------------------------|
| S1 | `DatabaseConnectionPoolExhausted` (payment-service, +2m50s), `HighErrorRate` (payment-service, +3m20s); `HighLatencyP95` pending on payment- and order-service |
| S2 | `PodOOMKilled` (order-service, +1m23s), `PodCrashLooping` (order-service, +3m23s); `DeploymentReplicasMismatch` pending on order-service |
| S4 | `DeploymentReplicasMismatch` (user-service, ~+5.5m: the rule's `for: 5m`) |
| S5 | `RedisDown` (+1m37s) |

S4 needed a rule fix: a rollout stuck on a nonexistent image keeps the old ReplicaSet serving, so *available == desired* and the original expression never fired. The rule now also fires on `kube_deployment_status_replicas_unavailable > 0` (with a promtool test for exactly this case).

How real and seeded alerts coexist is described in [alerts.md](alerts.md#real-alerts-and-seeded-alerts).

## Grafana (optional)

Provisioned from git, nothing is clicked together by hand:

- datasources `Prometheus` (uid `prometheus`, default) and `Elasticsearch logs` (uid `elasticsearch`, indices `*-prod-*,logs-k8s-*`);
- folder **AI Incident Agents** with **Service Overview** (`/d/aiops-service-overview`: RPS, 5xx ratio, p50/p95/p99, DB pool, Redis, RSS vs limit; variables `namespace`, `service`) and **K8s Workloads** (`/d/aiops-k8s-workloads`: desired vs available replicas, restarts, OOM kills, waiting reasons, pod phases).

Anonymous users get the Viewer role (the port is bound to 127.0.0.1); admin login is `admin` / `GRAFANA_ADMIN_PASSWORD`. Panel ids are stable because the agents build deep links to them (`viewPanel=<id>`).

## The `metrics` capability: prometheus-mcp and links (PR-021)

Agents read metrics only through `mcp-servers/prometheus-mcp` (127.0.0.1:8103, ADR-0009): `query`, `query_range`, `list_metrics`, `metric_metadata`, `get_targets`. Its guardrails are server-side: ≤ 24 h ranges, ≥ 15 s steps, ≤ 1,100 points and ≤ 50 series, a 20 s query timeout, no bare `{…}` selectors, and a metric allowlist (`METRICS_ALLOWLIST`).

```bash
make prometheus-mcp-up        # or make mcp-up (all MCP servers)
```

Portability lives in `config/environments/local.yaml` → `capabilities.metrics.settings`: metric names (`metrics.requests`, `metrics.latency_histogram`, …) and label names (`labels.service`, `labels.namespace`, …). Label *values* per service come from the service catalog (`metrics: {labels: {service: payment-service, namespace: prod}}`).

There is no Grafana MCP. Evidence links are built from two templates:
- `ui_link_template`: a Grafana dashboard panel (`/d/aiops-service-overview/...&viewPanel=6`), which opens once `make grafana-up` runs;
- `explore_link_template`: the exact PromQL in the always-on Prometheus UI.

## Memory

Measured with `docker stats` / `crictl stats` (PR-020, ~1,700 active series):

| Component | Memory | Limit |
|-----------|--------|-------|
| Prometheus after ~30 min of scraping | ~62 MiB (TSDB on disk: < 1 MB) | `mem_limit: 256m`, 2 days / 512 MB retention |
| kube-state-metrics (inside Minikube) | ~14 MB | 96 Mi |
| Grafana (optional) | ~205 MiB | `mem_limit: 320m` |
| prometheus-mcp (PR-021) | ~52 MiB | `mem_limit: 128m` |

Prometheus stays small because it scrapes only 5 targets every 30 s. If you add targets, keep an eye on `curl -s localhost:9090/api/v1/status/tsdb`.
