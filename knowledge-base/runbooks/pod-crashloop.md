---
title: Pod CrashLoopBackOff
type: runbook
services: [payment-service, order-service, user-service, inventory-service]
tags: [kubernetes, crashloopbackoff, restarts, probes, startup, config]
alerts: [PodCrashLooping, KubePodNotReady]
owner: platform-sre
last_reviewed: 2026-09-01
---
# Pod CrashLoopBackOff

## Summary
A container that exits repeatedly is restarted by Kubernetes with an exponential back-off (10 s, 20 s, 40 s ... up to 5 minutes). This is shown as `CrashLoopBackOff`. The pod never becomes ready, so capacity drops. The reason is almost always in the **previous** container's logs or in the pod events.

## Symptoms
- `kubectl get pods` shows `CrashLoopBackOff` and a rising RESTARTS count.
- Repeated `Starting <service>` lines in the logs, each followed by a fatal error.
- Ready replicas below desired; callers see HTTP 503.
- Alerts: `PodCrashLooping`, `KubePodNotReady`.

## Diagnosis
### 1. Read the last crash
- `kubectl logs <pod> -n prod --previous`: the last lines before the exit.
- `kubectl describe pod <pod> -n prod`: `Last State: Terminated`, `Reason` and `Exit Code`:
  - **137 / OOMKilled**: memory, see [memory-leak-oom](memory-leak-oom.md).
  - **1**: an application error at startup (bad config, missing env var or secret, migration failure).
  - **143**: SIGTERM, often a liveness probe killing a slow-starting app.

### 2. Configuration or code?
- Did the ConfigMap, Secret or image change recently? Check `kubectl rollout history` and diff the ReplicaSets.
- Do the dependencies needed at startup (database, Redis, secrets manager) respond?

### 3. Probes
- A liveness probe that is too aggressive (a short `initialDelaySeconds`) kills apps that are still starting. Compare the startup time in the logs with the probe settings.

## Mitigation
1. If it started with a release or config change, roll back ([bad-deployment-rollback](bad-deployment-rollback.md)).
2. Fix the missing config or secret and restart: `kubectl rollout restart deployment/<service> -n prod`.
3. For probe kills, add a `startupProbe` or raise `initialDelaySeconds` temporarily.

## Rollback
- `kubectl rollout undo deployment/<service> -n prod` and `kubectl rollout status deployment/<service> -n prod`.

## Escalation
- The owning team's on-call. If all pods on one node crash, suspect the node and page `#platform-oncall`.
