# K8s Agent — UC-07

`aiops agent run k8s "Orders are failing intermittently" -s order-service`

Checks a service's Kubernetes workload: replica status, pods (restarts, `OOMKilled`, `CrashLoopBackOff`, `ImagePullBackOff`), Warning events in the window, the rollout history (current vs previous revision, image, `kubernetes.io/change-cause`, rollout time) and the dependencies' deployments. Package: `backend/src/aiops/agents/k8s_agent/`
(agent `k8s`, capability `k8s`, evidence kind `k8s_event`, prompt `config/prompts/k8s/v1.md`). Data comes from the read-only `kubernetes-mcp` (PR-018, ADR-0008).

**Kubernetes shows symptoms and changes, not causes.** A rollout inside the window is a FACT; that it *caused* the incident is at most a CORRELATION/HYPOTHESIS. A FACT typed `root_cause` is downgraded to HYPOTHESIS. When the workload is healthy during an incident (S3), the agent says so instead of inventing a Kubernetes cause.

## How it works
1. **Scope (deterministic).** From the service catalog:
   - `k8s: {deployment, label_selector, container}` plus the per-environment `namespace` (default: `capabilities.k8s.settings.default_namespace`).
   - Each `depends_on` entry: a catalog service maps to its own `k8s.deployment`. Anything else (`postgres`, `redis`) is looked up as a deployment of that name in the same namespace.
2. **Tool calls (3–4).**
   - `get_deployment` (with 5 revisions of history)
   - `list_pods` (catalog label selector)
   - `list_events` (`type=Warning`, `object_name_prefix=<deployment>`, `since=<window start>`)
   - `list_deployments` (only if there are dependencies)

   Each call becomes evidence with a data-derived summary, timestamp and optional link (`settings.ui_link_template`, `{namespace} {kind} {name}`).
3. **Analysis (deterministic, `analysis.py`).** Each item below is computed from the data:

   | Signal | Rule |
   |--------|------|
   | `pod_restarts` | a container restarted, with its last termination inside the window (or unknown) |
   | `oom_killed` | a container's last/current termination reason is `OOMKilled` (in the window), or an `OOMKilling` event |
   | `crash_loop` | a container is waiting in `CrashLoopBackOff` (or `RunContainerError`/`CreateContainerError`), or a "Back-off restarting failed container" event |
   | `image_pull_error` | a container is waiting in `ImagePullBackOff`/`ErrImagePull`/`InvalidImageName`, or a pull failure event |
   | `replicas_unavailable` | ready or available < desired, **or `unavailableReplicas` > 0** (S4: the old ReplicaSet still serves 3/3 while the new pod can't pull) |
   | `probe_failures` | a liveness failure, or a readiness failure lasting longer than `probe_startup_grace_s` (60 s), **on a pod that is still serving** |
   | `dependency_unavailable` | a dependency deployment has ready < desired or is **scaled to 0** (S5: redis 0/0) |
   | `recent_rollout` | a revision of the deployment was created inside the window (compared with the previous revision: image changed, or a config/restart rollout) |
   | `dependency_rollout` | a dependency finished a rollout (`Progressing=NewReplicaSetAvailable`) inside the window (S3: inventory-service v1.4.2) |
   | `healthy` | none of the problem signals (the first seven), and every query succeeded |

   Rollout noise is not a signal. A new container fails its readiness probe for a few seconds after it starts, and a replaced pod fails while it shuts down. Both are counted and shown as "rollout noise" in the overview.
4. **LLM (bounded).**
   - It receives the overview and the eight read-only tools. Typical follow-ups are `get_pod_logs previous=true` for a crashed container, or `get_pod` for a memory limit.
   - Limits: `agents.k8s.limits` (4 steps, 8 tool calls, 45 s, 30k tokens).
5. **Finalize.**
   - Signals and status (`success` if a problem signal or a rollout, else `no_signal`) come from the data, so the LLM can neither invent nor hide one.
   - A data-derived headline always leads the summary, e.g. `user-service: 3/3 replicas ready (1 unavailable, 1/3 updated); ImagePullBackOff; rolled out revision 13 (v3.2.0: bump image) at …`.
   - Every signal gets a FACT/OBSERVATION finding that cites its evidence.

## Scenario ground truth (`scenarios/*/agents/k8s.yaml`)
All six fixtures were recorded **live** on Minikube (see below).

| Scenario | Service | Expected | Recorded signals |
|----------|---------|----------|------------------|
| S0 healthy | payment-service | `healthy`, `no_signal`, no problem signal | `healthy` (10-minute window with no fault and no rollout) |
| S1 DB pool | payment-service | `recent_rollout` (v1.8.2, same image: config rollout), pods healthy | `recent_rollout`, `healthy`: revision 14 (v1.8.2: tune db pool), same image |
| S2 memory leak | order-service | `oom_killed` + `pod_restarts` | `pod_restarts`, `oom_killed`, `crash_loop`, `replicas_unavailable`, `probe_failures`, `recent_rollout` (v2.3.0) |
| S3 slow dependency | order-service | order-service `healthy`; no invented cause (the inventory-service rollout is reported as `dependency_rollout`) | `healthy`, `dependency_rollout` (inventory-service v1.4.2) |
| S4 bad deployment | user-service | `image_pull_error` + `replicas_unavailable` + `recent_rollout` | `image_pull_error`, `replicas_unavailable` (3/3 ready, 1 unavailable, 1/3 updated), `recent_rollout` (v3.2.0) |
| S5 cache outage | payment-service | `dependency_unavailable` (redis scaled to 0) | `dependency_unavailable` (redis 0/0) |

## Live fixtures (`backend/tests/fixtures/k8s/<scenario>/`)
- **Files.** `k8s.json` holds the recorded MCP exchanges. `meta.json` holds the task window (`start`, `end`, `incident_start`), because live data can't be anchored at the fixed replay time. `aiops.evals.replay.replay_task` (used by the eval runner and the tests) replays with that window and passes `incident_start` as a hint.
- **Recorder.** `tests/fixtures/record_k8s.py` runs inside `aiops fault run S<n> -- …`, which holds the cluster lock across inject → settle → record → revert. S0 takes the lock itself and refuses to record while a fault is active.
  - The window starts 60 s before the injection, but never before the fault framework's previous baseline reset. A revert restarts every service, often seconds before the next injection, and that harness churn is not part of the incident.
- **Re-record:** `make record-k8s-fixtures` (from the main clone; ~15 min, waits for the lock).

## Tests
- `tests/unit/test_k8s_analysis.py`: the rules on hand-built data (OOM in/out of the window, pull errors with a serving old ReplicaSet, probe noise vs sustained failures, dependencies scaled to 0/missing/rolled out, partial data never `healthy`).
- `tests/unit/test_k8s_agent.py`: replay S0–S5 through the eval runner (6/6, zero tokens), the deterministic calls and prompt, a lying LLM on S0, a FACT root cause downgraded, and failures.
- `make eval AGENT=k8s MODE=replay`
- Integration: `tests/integration/test_k8s_agent_live.py` (the agent on the live cluster through kubernetes-mcp) and `tests/integration/test_k8s_mcp.py` (the PR-018 contract).

## Known limits
- **Events live ~1 hour** in Kubernetes. Older restarts are still visible through container `restartCount` and `lastState`, but their events are not.
- **No resource usage:** the lean cluster has no metrics-server. Memory against the limit comes from the Metrics agent. `get_pod` shows the limits.
- **Dependencies** are matched by deployment name in the same namespace. Managed services (RDS, ElastiCache) show as "not a Kubernetes workload here" and are left to the Metrics/Alert agents.
- **Rollout time** is the new ReplicaSet's creation time. A rollback that reuses an old ReplicaSet keeps the old creation time.
