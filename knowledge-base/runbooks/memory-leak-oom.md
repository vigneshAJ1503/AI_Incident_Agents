---
title: Memory leak and OOMKilled containers
type: runbook
services: [order-service, payment-service, user-service, inventory-service]
tags: [memory, oom, oomkilled, heap, gc, jvm, restarts, http-503]
alerts: [ContainerOOMKilled, PodRestartingFrequently, HighErrorRate]
owner: platform-sre
last_reviewed: 2026-09-01
---
# Memory leak and OOMKilled containers

## Summary
A process that keeps allocating memory it never frees eventually hits its limit. In the JVM services the heap fills up first: garbage collection runs more and more often, latency rises, and finally the JVM throws `java.lang.OutOfMemoryError`. If the container limit is lower than what the process needs, Kubernetes kills the container instead (**OOMKilled**, exit code 137). Either way the pod restarts. While it is down, callers get **HTTP 503**.

## Symptoms
- Log pattern: `GC overhead limit approaching: heap usage 97% after full GC` (a warning that precedes the crash).
- Log pattern: `java.lang.OutOfMemoryError: Java heap space`.
- Frequent startup lines (`Starting order-service ...`) for the same version: the service restarts every few minutes.
- Kubernetes: `Last State: Terminated, Reason: OOMKilled, Exit Code: 137`, and a growing restart count.
- Metrics: container memory climbs in a saw-tooth pattern, and every drop matches a restart.
- Callers see HTTP 503 or connection resets while the pod restarts.
- Alerts: `ContainerOOMKilled`, `PodRestartingFrequently`.

## Known issues
- **Per-request leak in order-service (INC-2026-044).** A request-scoped cache was stored in a static map and never evicted. It leaked about 5 MB per request (`LEAK_MB_PER_REQ`). Heap usage reached 97% about 15 minutes after a deploy, followed by `OutOfMemoryError: Java heap space` and a restart roughly every 4 minutes. Fix: roll back, then bound the cache (max entries plus TTL).

## Diagnosis
### 1. Confirm the restarts are memory-related
- `kubectl get pods -n prod -l app=<service>`: look at the RESTARTS column.
- `kubectl describe pod <pod> -n prod`: `Reason: OOMKilled` means the container limit was hit, and exit code 137 means SIGKILL.
- In the logs, look for `OutOfMemoryError` or `GC overhead` right before each `Starting <service>` line.

### 2. Leak or undersized limit?
- **Leak:** memory grows steadily with traffic and never plateaus, and it started with a release. Compare `container_memory_working_set_bytes` before and after the deploy.
- **Undersized:** memory plateaus close to the limit, and the service is killed during traffic peaks or batch jobs.
- Check whether a release happened just before the first OOM (`kubectl rollout history`).

### 3. Find the leak (after mitigating)
- Capture a heap dump before the kill (`-XX:+HeapDumpOnOutOfMemoryError`, or `jcmd <pid> GC.heap_dump`).
- Look at the largest retained objects: unbounded caches, static collections, listeners that are never removed.
- Review the diff of the suspect release for new caches, buffers or collections.

## Mitigation
1. **If a release introduced the leak, roll back.** See [bad-deployment-rollback](bad-deployment-rollback.md).
2. Buy time: raise the memory limit temporarily (for example 512Mi to 1Gi) and scale out, so that restarts don't line up across replicas.
3. Make sure the readiness probe fails quickly, so a pod that is about to die is taken out of the load balancer.
4. If a single endpoint triggers the leak, rate-limit or feature-flag that endpoint.

## Rollback
- `kubectl rollout undo deployment/<service> -n prod`, then check that memory stays flat for 15 minutes.
- Revert the memory limit change after the fix ships.

## Escalation
- The owning team's on-call (for example [order-service](../services/order-service.md): `#commerce-oncall`).
- If several services are OOMKilled at the same time, check node memory pressure and page `#platform-oncall`.

## Prevention
- Alert on `container_memory_working_set_bytes / limit > 0.9` for 10 minutes.
- Set `-XX:MaxRAMPercentage=75` so the heap leaves headroom inside the container.
- Soak-test releases for 30 minutes in staging and watch memory growth.
