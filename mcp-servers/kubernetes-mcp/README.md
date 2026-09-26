# kubernetes-mcp

A read-only, guard-railed Kubernetes MCP server for AI Incident Agents (PR-018, ADR-0008). It talks
to the Kubernetes REST API directly (no kubectl, no official client) as the read-only
`aiops-system/aiops-reader` ServiceAccount from `deploy/k8s/rbac`.

## Tools

| Tool | Purpose |
|------|---------|
| `list_namespaces` | The allowed namespaces and whether they exist |
| `list_pods` | Pods by `label_selector`: phase, `ready`, restarts, owner ReplicaSet, and per container the current state (`waiting` reason such as `ImagePullBackOff` / `CrashLoopBackOff`) and last state (`terminated` reason such as `OOMKilled`, exit code, times) |
| `get_pod` | One pod plus resources (requests/limits), probe targets and conditions |
| `list_events` | Events filtered by `object_name`, `object_name_prefix`, `object_kind`, `type` (`Warning`/`Normal`/`all`) and `since` (`30m` or ISO-8601); oldest first, the most recent `limit` kept, with counts by reason |
| `list_deployments` | Desired/updated/ready/available replicas, images, revision, `kubernetes.io/change-cause`, conditions |
| `get_deployment` | One deployment plus its **rollout history**: owned ReplicaSets by revision (newest first) with images, change-cause, creation (rollout) time, replicas, and `current` |
| `get_pod_logs` | The last `tail_lines` of a container's log (`previous=true` for the crashed instance), bounded by lines and bytes |
| `list_services` | Type, selector, ports, and ready / not-ready endpoint counts (from EndpointSlices) |

There is **no** tool that creates, updates, deletes, scales, restarts, execs, port-forwards or
reads Secrets/ConfigMaps.

## Guardrails (enforced server-side, for every client)
- **Read-only, twice:** the HTTP client only issues `GET`, and the ServiceAccount's RBAC only
  grants `get/list/watch` (no secrets, no configmaps, no `pods/exec`). `kubectl auth can-i` proves
  it (see the integration test).
- **Namespace allowlist:** every tool takes a `namespace` that must be in `ALLOWED_NAMESPACES`.
- **No secrets in results:** environment variables, `envFrom`, volumes, exec-probe commands and
  annotations outside a small allowlist (`kubectl.kubernetes.io/last-applied-configuration` holds
  the whole manifest) are never returned.
- **Credentials:** only bearer tokens (ServiceAccount kubeconfig or in-cluster). A kubeconfig with
  client certificates or exec/auth-provider plugins (typically an admin identity) is refused. The
  token is re-read on HTTP 401, so a refreshed short-lived token needs no restart.
- **Validation and caps:** object names must be RFC 1123 names, label selectors a small character
  set with at most 10 terms; `limit` ≤ `MAX_RESULTS`, events/ReplicaSets scanned ≤ `MAX_SCAN`,
  logs ≤ `MAX_LOG_LINES` lines and `MAX_LOG_BYTES` bytes, look-back ≤ `MAX_SINCE_HOURS`;
  `QUERY_TIMEOUT_S` per API request.

Kubernetes keeps events for about **1 hour** (`--event-ttl`); older events can't be seen.

## Configuration (env)

| Variable | Default |
|----------|---------|
| `KUBECONFIG` | (none: in-cluster ServiceAccount) |
| `K8S_CONTEXT` | the kubeconfig's current-context |
| `K8S_API_SERVER` | the kubeconfig's server (override, e.g. from the host) |
| `ALLOWED_NAMESPACES` | `prod` |
| `MAX_RESULTS` | `200` |
| `MAX_SCAN` | `2000` |
| `MAX_LOG_LINES` | `300` |
| `MAX_LOG_BYTES` | `64000` |
| `MAX_SINCE_HOURS` | `48` |
| `QUERY_TIMEOUT_S` | `15` |

## Run
```bash
make k8s-reader-kubeconfig   # RBAC + .data/k8s/aiops-reader.kubeconfig (token valid 24h)
make kubernetes-mcp-up       # container on 127.0.0.1:8106, kubeconfig mounted read-only
# or from the host (the kubeconfig points at the Minikube node IP, unreachable from macOS):
KUBECONFIG=../../.data/k8s/aiops-reader.kubeconfig \
K8S_API_SERVER=$(kubectl config view --minify --context aiops -o jsonpath='{.clusters[0].cluster.server}') \
  uv run kubernetes-mcp --transport http --port 8106
```

## Test
```bash
uv run pytest     # fake Kubernetes API (httpx2 MockTransport) + in-process MCP client
```
