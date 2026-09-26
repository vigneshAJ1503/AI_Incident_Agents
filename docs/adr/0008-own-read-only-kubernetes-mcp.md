# ADR-0008: Our own read-only Kubernetes MCP server, and a least-privilege ServiceAccount

- **Status:** Accepted
- **Date:** 2026-09-26

## Context
The K8s agent (UC-07, PR-019) needs, for one deployment: its replica status, its pods (phase,
restarts, `lastState.terminated.reason` such as `OOMKilled`, waiting reasons such as
`ImagePullBackOff`), the Warning events in a window, and the **rollout history** (current vs
previous ReplicaSet revision, image, `kubernetes.io/change-cause`, rollout time). MASTER_PLAN §16
requires a read-only ServiceAccount (no delete/patch/exec/secrets) and tool allowlists.

We evaluated **`containers/kubernetes-mcp-server`** (Go, v0.0.67 on 2026-09-18, image
`quay.io/containers/kubernetes_mcp_server`):
- **Tools:** `pods_list`, `pods_list_in_namespace`, `pods_get`, `pods_log`, `pods_top`,
  `events_list`, `namespaces_list`, `nodes_*`, the generic `resources_list` / `resources_get`,
  `configuration_view` (returns kubeconfig content), and the writes `pods_delete`, `pods_exec`,
  `pods_run`, `resources_create_or_update`, `resources_delete`, `resources_scale`, `helm_*`.
- **Safety flags:** `--read-only` and `--disable-destructive` hide the write tools, a TOML
  `denied_resources` list can block kinds such as Secrets, and `toolsets` narrows the groups.
- **Memory:** small (a single Go binary, roughly 30–50 MB).

It is a good general-purpose server, but for our use:
1. **No rollout-history tool.** Deployments and ReplicaSets are only reachable through the generic
   `resources_list(apiVersion, kind)`, which returns full objects (managedFields,
   `last-applied-configuration`, env) that the agent would have to post-process, and which an LLM
   has to parameterize correctly.
2. **No namespace allowlist or output caps** of its own: scoping relies entirely on RBAC, and log
   size on the caller.
3. **Tool names are not stable** (pre-1.0, `v0.0.x`; tools have been renamed and regrouped across
   releases), and our agents bind to exact tool names through the capability allowlist.
4. Its read-only mode is a **flag**: forgetting it (or a future default change) exposes exec and
   delete, protected only by RBAC.

## Decision
Build **`mcp-servers/kubernetes-mcp`** (MCP SDK v2 `MCPServer` + `httpx2` against the Kubernetes
REST API; no kubectl, no official client, so the image stays small) with eight read-only tools:
`list_namespaces`, `list_pods`, `get_pod`, `list_events`, `list_deployments`, `get_deployment`
(with rollout history from owned ReplicaSets), `get_pod_logs`, `list_services` (with ready
endpoint counts). Guardrails are enforced **server-side**:
- the HTTP client only issues `GET`; there is no write, exec, port-forward, Secret or ConfigMap tool;
- a **namespace allowlist** (`ALLOWED_NAMESPACES`), name and label-selector validation;
- **no secrets in results**: env/envFrom, volumes, exec-probe commands and non-allowlisted
  annotations are dropped;
- caps on list sizes, scanned events, log lines/bytes and look-back, and a per-request timeout;
- only **bearer-token** credentials: a kubeconfig with client certificates or exec plugins (an
  admin identity) is refused; the token is re-read on 401 so short-lived tokens can be rotated.

The identity is **`aiops-system/aiops-reader`** (`deploy/k8s/rbac`):
- `aiops-reader-workloads` (ClusterRole, bound by a **RoleBinding per investigated namespace**,
  only `prod` locally): `get/list/watch` on pods, pods/log, events (core and `events.k8s.io`),
  services, endpoints, endpointslices, deployments, replicasets, statefulsets, daemonsets;
- `aiops-reader-cluster` (ClusterRoleBinding): `get/list/watch` on nodes and namespaces.
- **Not granted:** Secrets; **ConfigMaps** (in real companies they routinely hold credentials and
  connection strings, so the default is no; the Code agent sees configuration changes through
  git, and a company can add a `configmaps` rule if its policy allows it); `pods/exec`,
  `pods/attach`, `pods/portforward`, `serviceaccounts/token`; every write verb.

`make k8s-reader-kubeconfig` writes `.data/k8s/aiops-reader.kubeconfig` (git-ignored) with a
token from `kubectl create token --duration=24h`, and the API server as reached from the `aiops`
Docker network (`https://172.21.0.100:8443`, which is in the API server certificate's SANs). The
compose service mounts it read-only, runs as UID 10001 with a read-only root filesystem, binds
`127.0.0.1:8106` and has `mem_limit: 128m` (≈53 MiB in use).

## Consequences
- Tool names and shapes are ours and stable; they map one-to-one onto the K8s agent's
  deterministic phase (deployment + history, pods, Warning events, dependencies).
- A write is denied **twice**: there is no write tool (and the capability allowlist has none), and
  RBAC refuses it (proven live by `tests/integration/test_k8s_mcp.py`: `kubectl auth can-i` plus a
  real `DELETE` with the reader token returning 403).
- We maintain about 1,000 lines of server code. Resource usage (`pods_top`) needs metrics-server, which the lean
  cluster doesn't run; CPU/memory usage comes from the Metrics agent (Prometheus) instead.
- Companies can still point the `k8s` capability at `kubernetes-mcp-server` (or a cloud vendor's
  server) with another allowlist, prompt version and an adapter for its tool names.
