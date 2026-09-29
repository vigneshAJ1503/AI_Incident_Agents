# Install on Kubernetes with Helm (PR-047)

The chart `deploy/helm/ai-incident-agents` installs the platform the way a company runs it:
the API, the Web UI, the Postgres evidence store, the migration Job and the read-only MCP
servers you switch on. Installing it for a company is **configuration only**:

1. **the profile** (capabilities, providers, limits) **and its service catalog**:
   inline in your values (rendered into a ConfigMap), or a ConfigMap you manage;
2. **Secrets you already have**, always by reference (`*.existingSecret`). The chart never
   holds a secret value, and `values.schema.json` has no field that could hold one;
3. **which MCP servers run in the cluster** (`mcpServers.<name>.enabled`).

No code changes, no image rebuilds (ADR-0011, [portability.md](../portability.md)).

## Try it on Minikube (zero cost, zero tokens)

```bash
minikube start -p aiops              # or make k8s-up (2.2 GB, docs/setup/zero-cost.md)
make helm-install-minikube           # build + load the images, create the Postgres Secret, install
kubectl --context aiops -n aiops port-forward svc/aiops-web 3100:3100   # http://localhost:3100
make helm-test-minikube              # helm test: /api/health through the web proxy
make helm-uninstall-minikube         # keeps the namespace, Secret and PVC
kubectl --context aiops delete namespace aiops   # ...and removes them
```

(`make demo` also uses port 3100 on the host; forward to another port, e.g. `3147:3100`, if
it is running.) `make helm-install-minikube` runs `scripts/helm.sh`, which needs only Docker,
`kubectl` and `minikube`: Helm runs in the pinned `alpine/helm:3.22.0` container on the `aiops`
Docker network (kubeconfig in `.data/helm/`, git-ignored). It builds the same images as
`make demo`, loads them into the node (`minikube image load`, no registry), creates the
`aiops-postgres` Secret with a random password (never printed) and installs with
[`deploy/helm/values-minikube.yaml`](../../deploy/helm/values-minikube.yaml): the default profile,
the demo history seeded by the migration Job, mock-tickets, and the NetworkPolicies on.

### Verified live (2026-09-29, Minikube v1.37 `aiops`, Docker Desktop, Apple silicon)

- `helm upgrade --install`: all pods Ready, the post-install migration Job completed in 9 s
  (`aiops db upgrade` + `aiops demo seed`: 46 demo investigations); a second
  `helm upgrade` ran the pre-upgrade migration (7 s); `helm test` passed
  (`{"status": "ok", "profile": "helm", "store": "ok"}`).
- Through `kubectl port-forward svc/aiops-web`: the UI (`/`, a report page) 200, `/api/health`
  `ok` (profile `helm`, store `ok`, tickets `ok`, LLM not configured), the dashboard over 46
  investigations.
- A new S1 investigation (`POST /api/investigations`, replay): completed in 12 s, severity
  `critical`, confidence 0.95, **0 tokens, $0**. Its Jira draft was approved and executed
  through mock-tickets-mcp: `OPS-1` created (approvals and audit in Postgres).
- NetworkPolicy enforced by Minikube's CNI (kindnet): an unlabelled pod could reach the web
  but not the API or Postgres.
- Memory (`crictl stats` in the node): api 183 MB, postgresql 107 MB (incl. page cache),
  web 66 MB, mock-tickets 66 MB: **≈ 0.42 GB** for the release. The whole Minikube node was
  1.87 GiB of its 2.15 GiB, next to the running `make demo` + Prometheus + Grafana (~0.7 GB):
  ≈ 2.6 GB in Docker, inside the 5 GB budget.

## Install for a company

### 1. Secrets (create them first; names are yours)

```bash
NS=aiops; kubectl create namespace $NS
# Bundled Postgres password (skip with an external database, see below).
kubectl -n $NS create secret generic aiops-postgres --from-literal=password="$(openssl rand -hex 24)"
# The hosted LLM: every key becomes an env var of the API, named as your profile reads it.
kubectl -n $NS create secret generic aiops-llm \
  --from-literal=OPENAI_COMPAT_API_KEY=<key> --from-literal=OPENAI_COMPAT_API_KEY_2=<key2>
# API authentication: named keys = approver identities (docs/api/README.md#security).
UI_KEY=$(openssl rand -hex 24)
kubectl -n $NS create secret generic aiops-api-auth \
  --from-literal=AIOPS_API_KEYS="ui:${UI_KEY},oncall:$(openssl rand -hex 24)" \
  --from-literal=AIOPS_UI_API_KEY="${UI_KEY}"
# Tokens of the data sources your MCP servers use, e.g. Prometheus/Mimir.
kubectl -n $NS create secret generic aiops-prometheus --from-literal=PROM_BEARER_TOKEN=<token>
```

Use your secret manager's operator (External Secrets, Sealed Secrets, Vault) to create the
same Secrets; the chart only references them.

### 2. Values

```yaml
# acme-values.yaml
profile:
  name: acme
  profileYaml: |
    extends: local-k8s          # or write a full profile: profiles/_template/profile.yaml
    metadata: {company: ACME}
    llm:
      provider: openai_compat
      api_key: ${OPENAI_COMPAT_API_KEY}
      models: {fast: llama-3.1-8b-instant, agent: openai/gpt-oss-20b, rca: openai/gpt-oss-120b}
    storage: {approvals: postgres, audit: postgres}
  servicesYaml: |
    services:
      - name: checkout
        description: Checkout API
        owners: [payments]
        tier: 1
llm:
  existingSecret: aiops-llm
api:
  auth: {existingSecret: aiops-api-auth}
web:
  apiKeySecret: {name: aiops-api-auth, key: AIOPS_UI_API_KEY}
mcpServers:
  prometheus:
    enabled: true
    existingSecret: aiops-prometheus
    env: {PROM_URL: https://mimir.acme.internal/prometheus, METRIC_ALLOWLIST: "http_.*,kube_.*,up"}
  kubernetes: {enabled: true}
k8sReader: {namespaces: [prod], clusterRead: true}
ingress:
  enabled: true
  className: nginx
  host: aiops.acme.internal
  annotations: {nginx.ingress.kubernetes.io/proxy-buffering: "off"}   # SSE
  tls: [{secretName: aiops-tls, hosts: [aiops.acme.internal]}]
networkPolicy: {enabled: true}
```

```bash
helm upgrade --install aiops deploy/helm/ai-incident-agents -n aiops -f acme-values.yaml --wait
helm test aiops -n aiops
```

A profile kept in a private repo: create the ConfigMap yourself (keys `profile.yaml`,
`services.yaml`) and set `profile.existingConfigMap` (+ `profile.name`). A profile baked into
the image (`local`, `local-k8s`, `local-loki`): `profile.create: false`.

### External Postgres (recommended in production)

```bash
kubectl -n aiops create secret generic aiops-db \
  --from-literal=url='postgresql://aiops:<password>@db.acme.internal:5432/aiops?sslmode=require'
```
```yaml
postgresql: {enabled: false}
externalDatabase: {existingSecret: aiops-db, urlKey: url}
```
The API gets `AIOPS_DATABASE_URL`, mock-tickets `TICKETS_DATABASE_URL`, knowledge-mcp
`KNOWLEDGE_DATABASE_URL`, all from that one Secret key.

## What the chart deploys

| Component | Default | Details |
|---|---|---|
| `api` Deployment + Service | on | `aiops serve`; startup + liveness = TCP (the process serves), readiness = `/api/health` (a slow data source never restarts it); 60 s graceful shutdown; 100m/192Mi requests, 1 CPU/384Mi limits |
| `web` Deployment + Service | on | the only entry point: serves the UI and proxies `/api/*` (REST + SSE) to the API service; readiness `/`, liveness TCP; 64Mi/192Mi |
| Ingress | off | for the web only (add `proxy-buffering: off` for SSE on nginx) |
| `postgresql` StatefulSet + PVC | on | one `postgres:16.15-alpine3.24`, UID 70, 2 Gi, `shared_buffers=32MB`; no backups/replication: use `externalDatabase` in production |
| `migrate` Job | on | Alembic `aiops db upgrade` as a Helm hook: `post-install` (retries until the bundled Postgres answers) and `pre-upgrade`; `migrations.useHelmHooks: false` = a plain Job per revision (GitOps); `seedDemo` adds the demo history |
| MCP servers | mock-tickets on, the rest off | one toggle per server; the API gets `<urlEnv>=http://<release>-mcp-<name>:<port>/mcp` (`LOGS_MCP_URL`, `METRICS_MCP_URL`, ...), the names the built-in profiles read; only one server per `urlEnv` (the chart fails with a readable error otherwise) |
| `k8s-reader` ServiceAccount + RBAC | with the kubernetes server | get/list/watch on pods, logs, events, services, endpoints(-slices), deployments, replicasets, statefulsets, daemonsets in `k8sReader.namespaces` (RoleBindings) + optional nodes/namespaces; never secrets, configmaps, exec, port-forward or writes (ADR-0008). The only pod with a token |
| NetworkPolicy | off | ingress allowlists: web ← anyone/`webFrom`, api ← web (+ `apiFrom`, `helm test`), MCP servers ← api, Postgres ← `aiops.io/db-client` pods. Egress stays open (hosted LLM, your data sources) |
| PodDisruptionBudget | off | `minAvailable` for api and web; use with `replicaCount >= 2` |
| `helm test` pod | on | `/api/health` through the web proxy, `store == ok` |

### Security, everywhere

- Pods: `runAsNonRoot`, numeric UID/GID 10001 (Postgres 70), `fsGroup`,
  `seccompProfile: RuntimeDefault`, `automountServiceAccountToken: false` (except the
  k8s reader). The default ServiceAccount has no RBAC at all.
- Containers: `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, all capabilities
  dropped, CPU and memory limits. Writable paths are `emptyDir`s with a size limit: `/tmp`
  everywhere, `/app/.data` (API: the JSONL audit fallback), `/app/.next/cache` (web), the
  socket dir and the data volume (Postgres).
- Secrets only through `secretKeyRef`/`envFrom` of Secrets you created.
- `values.schema.json`: unknown keys, non-string env values, `latest` tags, a Secret *value*
  where a Secret *name* belongs, a writable root FS or root user in the security contexts
  all fail `helm install` with a readable message.
- `make helm-lint` (CI job `helm`): `helm lint --strict`, then every values set
  (defaults, `ci/*.yaml`, `values-minikube.yaml`) rendered and validated with
  `kubeconform -strict` against the Kubernetes 1.36 schemas, then
  [`scripts/helm_guardrails.py`](../../scripts/helm_guardrails.py) on the rendered manifests:
  the security context above on every workload, limits, probes, pinned tags, no rendered
  `Secret`, no inline value for a secret-looking env var.

## The production images

The same Dockerfiles serve `make demo` (compose) and the chart. All of them:

| | |
|---|---|
| Bases | pinned (PR-042c): `python:3.12.13-slim-trixie` + Debian security updates; `node:24.19.0-alpine3.23` + Alpine fixes |
| User | numeric `10001:10001` |
| Shell / package manager | **none at runtime**: `sh`/`bash`/`dash`, `perl`, `apt`/`dpkg` (Python) and BusyBox + `apk` (web) are removed in the last layer; the package databases stay, so Trivy still scans every package. `HEALTHCHECK`s are exec-form, the web image runs `node server.js` without the base image's shell entrypoint |
| Filesystem | runs with a read-only root; `/tmp` (+ `/app/.data` for the API, `/app/.next/cache` for the web) writable |
| Labels | OCI `org.opencontainers.image.{title,description,source,licenses,version,revision}` (`--build-arg VERSION=… REVISION=$(git rev-parse --short HEAD)`) |
| Scans | Trivy on every image in `security.yml` (fixable HIGH/CRITICAL fail) |

Image names are `aiops/<name>:<appVersion>` (`backend`, `web`, `<server>-mcp`). Push them to your
registry and set `global.imageRegistry` (+ `global.imagePullSecrets`).

## Operations

- **Upgrades:** `helm upgrade` runs the migration Job first (pre-upgrade); the API also
  migrates on start, and a Postgres advisory lock (`pg_advisory_xact_lock`) serializes the two.
  Profile changes roll the API (a checksum annotation).
- **Uninstall:** `helm uninstall` keeps the PVC and your Secrets (and, as Helm does for hook
  resources, the last migration Job until its TTL and the `helm test` pod). Delete the
  namespace to remove everything.
- **Scaling the API:** keep `replicaCount: 1` for now. Approvals and the audit are in Postgres
  (the chart's default profile), but an investigation runs in the process that started it,
  and rate limits / idempotency keys are in-process (PR-042b); multi-replica is a follow-up.
- **Prompt overrides:** a flat ConfigMap cannot hold `prompts/<agent>/v1.md`. Ship the whole
  profile folder instead (a volume kept in sync from your private repo, via `api.extraVolumes` /
  `api.extraVolumeMounts`), set `profile.create: false` and point
  `api.env.AIOPS_PROFILES_DIR` at it.
